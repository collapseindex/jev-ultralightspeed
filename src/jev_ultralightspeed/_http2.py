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
import threading
import time
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


def in_a_loop() -> bool:
    """True when the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Pipe:
    """A background event loop holding one HTTP/2 client open."""

    def __init__(self, url: str, key: str, *, inflight: int, timeout: float,
                 retry_statuses, max_retries: int) -> None:
        self.url = url
        self.inflight = max(1, inflight)
        self.timeout = timeout
        self.retry_statuses = retry_statuses
        self.max_retries = max_retries
        self._headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._gate: asyncio.Semaphore | None = None
        self._thread = threading.Thread(target=self._run, name="jev-http2", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    # -- the loop ----------------------------------------------------------
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        limits = httpx.Limits(max_connections=self.inflight,
                              max_keepalive_connections=self.inflight)
        self._client = httpx.AsyncClient(http2=True, timeout=self.timeout, limits=limits,
                                         headers=self._headers)
        self._gate = asyncio.Semaphore(self.inflight)
        self._ready.set()
        self._loop.run_forever()

    async def _one(self, body: dict, on_request, on_timing, on_retry) -> dict:
        from . import JevError

        async with self._gate:
            for attempt in range(self.max_retries):
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
                    if on_retry:
                        on_retry()
                except httpx.HTTPError as error:
                    if on_timing:
                        on_timing((time.monotonic() - started) * 1000)
                    if attempt == self.max_retries - 1:
                        raise JevError(f"could not reach Jev: {error}") from error
                    if on_retry:
                        on_retry()
                await asyncio.sleep(min(30.0, 2 ** attempt))
            raise JevError("out of retries")

    async def _all(self, bodies, on_request, on_timing, on_retry) -> list[dict]:
        return list(await asyncio.gather(*(self._one(body, on_request, on_timing, on_retry)
                                           for body in bodies)))

    # -- from ordinary code ------------------------------------------------
    def ask_all(self, bodies: Sequence[dict], *, on_request: Callable | None = None,
                on_timing: Callable | None = None, on_retry: Callable | None = None) -> list[dict]:
        future = asyncio.run_coroutine_threadsafe(
            self._all(bodies, on_request, on_timing, on_retry), self._loop)
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
        self._loop = None
