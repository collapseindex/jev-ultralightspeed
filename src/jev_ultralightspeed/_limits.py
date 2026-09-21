"""How often a request may go out."""

from __future__ import annotations

import threading
import time
from collections import deque


class _Limiter:
    """
    A sliding window, shared by every worker, and optionally an even pace.

    The window is a deque that old stamps are popped off the front of. It used to
    be a list rebuilt from scratch on every permit, which at a thousand a minute
    meant walking a thousand timestamps to hand out one.

    Pacing puts a floor under the gap between sends. The window alone allows a
    thousand requests in the first second of a minute and then nothing, which is
    fine by the published ceiling and not always fine by the service.
    """

    def __init__(self, per_minute: int, *, paced: bool = False) -> None:
        self.per_minute = max(1, per_minute)
        self._recent: deque[float] = deque()
        self._interval = 60.0 / self.per_minute if paced else 0.0
        self._next_send = 0.0
        self._lock = threading.Lock()

    def try_take(self) -> float:
        """
        0.0 when a permit was taken, otherwise how long until one frees up.

        Asking without blocking is what lets the fast path wait in slices and
        notice that its run has been abandoned, rather than draining a queue of
        permits nobody is going to use.
        """
        with self._lock:
            now = time.monotonic()
            while self._recent and now - self._recent[0] >= 60.0:
                self._recent.popleft()
            wait = max(0.0, self._next_send - now)
            if len(self._recent) >= self.per_minute:
                wait = max(wait, 60.0 - (now - self._recent[0]))
            if wait > 0.0:
                return max(0.001, wait)
            self._recent.append(now)
            # Set, not advanced: waking after a quiet spell must not release a
            # burst of everything the pace would have allowed while nothing ran.
            self._next_send = now + self._interval
            return 0.0

    def take(self) -> None:
        """The blocking form, for the threaded transport."""
        while True:
            wait = self.try_take()
            if wait <= 0.0:
                return
            time.sleep(wait)
