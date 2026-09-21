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
import datetime
import email.utils
import random
import ssl
import threading
import time
from typing import Callable, Sequence

from ._errors import JevError

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


KEEPALIVE_S = 60.0              # httpx drops an idle connection after 5s by default, which
                                # makes every call in an intermittent job pay the handshake
ADMIT_SLICE_S = 0.25            # how long to wait for room before looking up again
MAX_WAIT_S = 60.0               # the longest wait this client invents for itself
MAX_HINT_S = 300.0              # the longest it will sit on one the server asked for
HINT_JITTER_S = 1.0             # spread on top of the server's own number


def _retry_after_seconds(hint: str | None) -> float | None:
    """
    Seconds out of a Retry-After header, which RFC 9110 allows to be either a
    count or an HTTP date. Only the count was understood before, so a server
    answering with a date was treated as having said nothing.
    """
    if not hint:
        return None
    hint = hint.strip()
    try:
        return max(0.0, float(hint))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(hint)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:                         # a date without a zone is UTC
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def _backoff(attempt: int, retry_after: str | None) -> float:
    """
    How long to wait. The server's own Retry-After is a minimum rather than an
    answer: every worker given the same hint would otherwise come back at the
    same instant, so jitter goes on top of it, never underneath.

    What is not capped is the hint. Clamping "wait two minutes" to sixty seconds
    meant coming back early, against the one instruction the server gave, which
    is how a throttle turns into a ban. Only the wait this client invents for
    itself is bounded. A hint longer than `MAX_HINT_S` is refused rather than
    quietly shortened or silently slept on, because at that point the answer is
    to come back later rather than to hold a process open.
    """
    hint = _retry_after_seconds(retry_after)
    if hint is not None:
        if hint > MAX_HINT_S:
            raise JevError(f"the server asked for {hint:.0f}s before retrying, which is longer "
                           f"than the {MAX_HINT_S:.0f}s this client will wait. Try again later.")
        return hint + random.random() * HINT_JITTER_S
    return min(MAX_WAIT_S, min(30.0, 2 ** attempt) * (0.5 + random.random()))


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
                 retry_statuses, max_retries: int, limiter,
                 verify=None, on_protocol=None) -> None:
        self.url = url
        self.verify = verify if verify is not None else ssl.create_default_context()
        # ALPN has to be on the context, or h2 is never offered and the whole
        # reason for this module quietly becomes HTTP/1.1. httpx sets it on the
        # contexts it builds itself and not on one it is handed, so a caller
        # bringing their own trust for a private gateway would lose the fast path
        # without being told.
        try:
            self.verify.set_alpn_protocols(["h2", "http/1.1"])
        except (AttributeError, NotImplementedError):     # not a context we can steer
            pass
        self.on_protocol = on_protocol
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
        self._thread = threading.Thread(target=self._run, name="jev-http2", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    # -- the loop ----------------------------------------------------------
    async def _admit(self, run=None) -> None:
        """
        Wait for room under the request ceiling.

        Two things matter here. The permit is taken immediately before the send,
        so what the limiter records is when the request actually went out rather
        than when some coroutine first thought about it: with a hundred bodies
        and two slots, every permit used to be spent by the third send. And the
        wait happens in slices, so a run that has been abandoned stops waiting
        instead of draining its whole queue of permits first.
        """
        while True:
            if run is not None and run.broken:
                raise JevError(run.broken)
            wait = self.limiter.try_take()
            if wait <= 0.0:
                return
            await asyncio.sleep(min(wait, ADMIT_SLICE_S))

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        limits = httpx.Limits(max_connections=self.inflight,
                              max_keepalive_connections=self.inflight,
                              keepalive_expiry=KEEPALIVE_S)
        # Trust the same certificates the standard library does, which means the
        # operating system's store. httpx would otherwise trust only certifi's
        # bundle, and fail on any machine whose TLS is inspected by a proxy or
        # an antivirus whose root lives in the OS store.
        self._client = httpx.AsyncClient(http2=True, timeout=self.timeout, limits=limits,
                                         headers=self._headers,
                                         verify=self.verify)
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
        for attempt in range(self.max_retries):
            if run.broken:
                raise JevError(run.broken)          # abandoned before this one even waited
            wait = None
            pushed = 0
            # The ceiling is taken before the slot, so a request waiting for
            # the rate limit is not sitting on one of the few in-flight slots.
            async with self._gate:
                # Checked again, and this is the one that matters: every
                # coroutine passed the check above before anything had failed.
                if run.broken:
                    raise JevError(run.broken)
                await self._admit(run)          # the permit and the send are one moment
                started = time.monotonic()
                try:
                    answer = await self._client.post(self.url, json=body)
                    if self.on_protocol:
                        self.on_protocol(answer.http_version)   # h2, or the quiet fallback
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
                    pushed = answer.status_code
                except httpx.HTTPError as error:
                    if on_timing:
                        on_timing((time.monotonic() - started) * 1000)
                    if attempt == self.max_retries - 1:
                        raise JevError(f"could not reach Jev: {error}") from error
                    wait = _backoff(attempt, None)
                    pushed = 0
            if on_retry:
                on_retry(pushed, wait)
            await asyncio.sleep(wait)               # the slot is free while this waits
        raise JevError("out of retries")

    async def _all(self, bodies, on_request, on_timing, on_retry, on_done,
                   on_failed=None) -> list[dict]:
        """
        Every request runs to its own end rather than being cancelled by a
        sibling, but a run that is plainly doomed is abandoned early: a wrong
        key would otherwise send every request, be refused by every one of
        them, and take half an hour to say so.

        `on_done(index, payload)` is called the moment a request lands, on this
        loop's thread. It is what makes progress live, a checkpoint durable and
        a half-finished run recoverable, so the caller reads each payload there
        and this returns them only for the ordinary case. An exception from it
        fails that request, exactly as a bad payload would on the threaded path.

        `on_failed(index, error)` says what to do with a request that failed for
        good. Returning True carries on without it, which is how a job of a
        million rows finishes despite a handful of bad ones. Returning False, or
        not being given, abandons the whole run there and then: nothing still
        queued is sent, because the call is going to raise regardless and a wrong
        key over a million rows would otherwise be refused a million times.
        """
        run = _Run()                    # per call, so two callers cannot stomp each other

        async def one(index, body):
            try:
                answer = await self._one(body, run, on_request, on_timing, on_retry)
                if on_done:
                    on_done(index, answer)  # while the rest are still in flight
                return answer
            except Exception as error:
                run.failures += 1
                if on_failed is not None and on_failed(index, error):
                    return None             # carried, and the caller has marked those items
                if not run.broken:
                    run.broken = (f"abandoned after {run.failures} failures, "
                                  f"the first being: {error}")
                raise

        answers = await asyncio.gather(*(one(index, body) for index, body in enumerate(bodies)),
                                       return_exceptions=True)
        for answer in answers:
            if isinstance(answer, BaseException):
                raise answer                 # what did land is already with the caller
        return list(answers)

    # -- from ordinary code ------------------------------------------------
    def ask_all(self, bodies: Sequence[dict], *, on_request: Callable | None = None,
                on_timing: Callable | None = None, on_retry: Callable | None = None,
                on_done: Callable | None = None,
                on_failed: Callable | None = None) -> list[dict]:
        future = asyncio.run_coroutine_threadsafe(
            self._all(bodies, on_request, on_timing, on_retry, on_done, on_failed), self._loop)
        return future.result()

    def submit(self, body: dict, run, *, on_request: Callable | None = None,
               on_timing: Callable | None = None, on_retry: Callable | None = None):
        """
        One request, as a future the calling thread can wait on.

        `ask_all` is the batch form and it cannot be handed answers one at a
        time. A rolling pipeline needs this: a bounded number of requests in
        flight, each landing on its own, so a stream gives answers back as they
        arrive instead of a chunk at a time. `run` is shared across the pipeline,
        which is how abandoning one stops the rest.
        """
        self._ready.wait(timeout=10)
        return asyncio.run_coroutine_threadsafe(
            self._one(body, run, on_request, on_timing, on_retry), self._loop)

    def new_run(self):
        """A fresh piece of per-pipeline state, for the rolling path."""
        return _Run()

    def warm(self) -> None:
        """Open the connection before any work arrives, on the same budget."""
        async def touch():
            await self._admit()                     # a request is a request
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
        self._loop = None
