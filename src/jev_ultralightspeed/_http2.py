"""
The fast transport: one event loop, one HTTP/2 connection, many streams.

Measured against a thread per connection on HTTP/1.1, this is about twice the
throughput once requests are packed, because every request in flight shares a
single connection instead of needing one each. It needs httpx and h2, so it is
optional: without them the client falls back to the standard library and still
works.

    pip install "jev-ultralightspeed[fast]"

The loop lives on a thread of its own and stays there, so the connection and
its HTTP/2 session are set up once and reused by every later call. Building
them per call costs more than the multiplexing saves.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence

try:                                        # optional, and checked before use
    import httpx
except ImportError:                          # pragma: no cover - depends on install
    httpx = None


def available() -> bool:
    """True when httpx is importable with HTTP/2 support."""
    if httpx is None:
        return False
    try:
        import h2                            # noqa: F401
    except ImportError:                      # pragma: no cover - depends on install
        return False
    return True


# How many failures before a run is abandoned rather than sending the rest.
GIVE_UP_AFTER = 5


def _backoff(attempt: int, retry_after: str | None) -> float:
    """
    How long to wait. The server's own Retry-After wins; otherwise doubling
    with jitter, because eight workers backing off on the same schedule
    collide again by construction.
    """
    if retry_after:
        try:
            return max(0.0, min(60.0, float(retry_after)))
        except ValueError:
            pass                                    # a date, not seconds: fall through
    return min(30.0, 2 ** attempt) * (0.5 + random.random())


def in_a_loop() -> bool:
    """True when the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Run:
    """One call's worth of state: how many have failed, and whether to stop."""

    __slots__ = ("failures", "broken")

    def __init__(self) -> None:
        self.failures = 0
        self.broken = ""


class Pipe:
    """A background event loop holding one HTTP/2 client open."""

    def __init__(self, url: str, key: str, *, inflight: int, timeout: float,
                 retry_statuses, max_retries: int, limiter) -> None:
        self.url = url
        self.inflight = max(1, inflight)
        self.timeout = timeout
        self.retry_statuses = retry_statuses
        self.max_retries = max_retries
        # The client's own limiter, shared, so the two transports cannot each
        # hold a separate window and add up to twice the ceiling.
        self.limiter = limiter
        self._headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._gate: asyncio.Semaphore | None = None
        self._waiters = ThreadPoolExecutor(max_workers=max(2, self.inflight),
                                           thread_name_prefix="jev-limit")
        self._thread = threading.Thread(target=self._run, name="jev-http2", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    # -- the loop ----------------------------------------------------------
    async def _wait_for_the_ceiling(self) -> None:
        """
        The limiter blocks, so it waits on this pipe's own threads rather than
        the default executor, which belongs to whoever imported us.
        """
        await self._loop.run_in_executor(self._waiters, self.limiter.take)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        limits = httpx.Limits(max_connections=self.inflight,
                              max_keepalive_connections=self.inflight)
        # Trust the same certificates the standard library does, which means the
        # operating system's store. httpx would otherwise trust only certifi's
        # bundle, and fail on any machine whose TLS is inspected by a proxy or
        # an antivirus whose root lives in the OS store.
        self._client = httpx.AsyncClient(http2=True, timeout=self.timeout, limits=limits,
                                         headers=self._headers,
                                         verify=ssl.create_default_context())
        self._gate = asyncio.Semaphore(self.inflight)
        self._ready.set()
        self._loop.run_forever()

    async def _one(self, body: dict, run, on_request, on_timing, on_retry) -> dict:
        """
        One request, with its waiting done outside the semaphore.

        A request sleeping off a 429 used to hold one of the few in-flight
        slots while doing nothing, which is throughput thrown away exactly
        when there is least of it to spare.
        """
        from . import JevError

        for attempt in range(self.max_retries):
            if run.broken:
                raise JevError(run.broken)          # abandoned before this one even waited
            wait = None
            # The ceiling is taken before the slot, so a request waiting for
            # the rate limit is not sitting on one of the few in-flight slots.
            await self._wait_for_the_ceiling()
            async with self._gate:
                # Checked again, and this is the one that matters: every
                # coroutine passed the check above before anything had failed.
                if run.broken:
                    raise JevError(run.broken)
                started = time.monotonic()
                try:
                    answer = await self._client.post(self.url, json=body)
                    if on_timing:
                        on_timing((time.monotonic() - started) * 1000)
                    if answer.status_code == 200:
                        data = answer.json()
                        if on_request:
                            on_request(data)
                        return data
                    if answer.status_code not in self.retry_statuses or attempt == self.max_retries - 1:
                        raise JevError(f"Jev answered {answer.status_code}: {answer.text[:300]}")
                    wait = _backoff(attempt, answer.headers.get("retry-after"))
                except httpx.HTTPError as error:
                    if on_timing:
                        on_timing((time.monotonic() - started) * 1000)
                    if attempt == self.max_retries - 1:
                        raise JevError(f"could not reach Jev: {error}") from error
                    wait = _backoff(attempt, None)
            if on_retry:
                on_retry()
            await asyncio.sleep(wait)               # the slot is free while this waits
        raise JevError("out of retries")

    async def _all(self, bodies, on_request, on_timing, on_retry, on_failure) -> list[dict]:
        """
        Every request runs to its own end rather than being cancelled by a
        sibling, but a run that is plainly doomed is abandoned early: a wrong
        key would otherwise send every request, be refused by every one of
        them, and take half an hour to say so.
        """
        run = _Run()                    # per call, so two callers cannot stomp each other

        async def one(body):
            try:
                return await self._one(body, run, on_request, on_timing, on_retry)
            except Exception as error:
                run.failures += 1
                if run.failures >= GIVE_UP_AFTER and not run.broken:
                    run.broken = f"abandoned after {run.failures} failures, the first being: {error}"
                raise

        answers = await asyncio.gather(*(one(body) for body in bodies), return_exceptions=True)
        done = [a for a in answers if not isinstance(a, BaseException)]
        for answer in answers:
            if isinstance(answer, BaseException):
                if on_failure:
                    on_failure(done)             # hand back what did arrive
                raise answer
        return list(answers)

    # -- from ordinary code ------------------------------------------------
    def ask_all(self, bodies: Sequence[dict], *, on_request: Callable | None = None,
                on_timing: Callable | None = None, on_retry: Callable | None = None,
                on_failure: Callable | None = None) -> list[dict]:
        future = asyncio.run_coroutine_threadsafe(
            self._all(bodies, on_request, on_timing, on_retry, on_failure), self._loop)
        return future.result()

    def warm(self) -> None:
        """Open the connection before any work arrives."""
        async def touch():
            try:
                await self._client.get(self.url)          # a 404 or 405 is fine: it connects
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(touch(), self._loop).result(timeout=self.timeout)

    def close(self) -> None:
        if self._loop is None:
            return
        async def shut():
            await self._client.aclose()
        try:
            asyncio.run_coroutine_threadsafe(shut(), self._loop).result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._waiters.shutdown(wait=False)
        self._loop = None
