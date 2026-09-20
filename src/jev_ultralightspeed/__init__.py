"""
jev-ultralightspeed: classify a pile of items with Jev, fast.

The naive loop sends one item per request and waits for each answer, which
spends most of its time on round trips and re-sends the same overhead every
time. Three changes, none of them clever, together give roughly twenty times
the throughput for a little over a third of the tokens:

  pack        several items into one request, one question each
  parallel    several requests in flight, under the published rate limit
  reuse       one connection, kept open, multiplexed where possible
  dedupe      identical text asked once

The standard library alone gets you a thread per connection on HTTP/1.1. With
httpx and h2 installed (`pip install "jev-ultralightspeed[fast]"`) every
request in flight shares one HTTP/2 connection instead, which measured about
twice as fast. Either way, bring your own key.

    from jev_ultralightspeed import classify

    answers = classify(messages, "Does this need a human today?")
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import _http2

__version__ = "0.2.0"

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Measured, not guessed: eight items per request was the sweet spot, and four
# requests in flight kept latency flat. See bench.py, which reproduces it.
PACK = 8
WORKERS = 4

# TypeSafe publishes 1,200 requests a minute. Stay under it by default.
REQUESTS_PER_MINUTE = 1_000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRIES = 5
TIMEOUT_S = 60.0
MAX_ITEM_CHARS = 20_000


class JevError(RuntimeError):
    """The API said no, or said something this client cannot read."""


@dataclass
class Answer:
    """One item's answer: the probability, and what it works out to."""

    item: str
    p: float                      # probability of true, for a yes/no question
    label: str                    # "yes" or "no", or the chosen option
    kind: str = "noul"
    distribution: dict = field(default_factory=dict)
    confidence: float | None = None

    @property
    def yes(self) -> bool:
        return self.label == "yes"


@dataclass
class Usage:
    """What a run cost, so a claim about speed can be checked."""

    items: int = 0
    requests: int = 0            # requests that came back with an answer
    retries: int = 0             # attempts that failed and were sent again
    input_tokens: int = 0
    output_tokens: int = 0
    cached: int = 0
    seconds: float = 0.0

    USD_PER_MILLION_INPUT = 0.042        # TypeSafe's published price

    @property
    def items_per_second(self) -> float:
        return self.items / self.seconds if self.seconds else 0.0

    @property
    def tokens_per_item(self) -> float:
        return (self.input_tokens + self.output_tokens) / self.items if self.items else 0.0

    @property
    def usd(self) -> float:
        return self.input_tokens * self.USD_PER_MILLION_INPUT / 1e6

    def __str__(self) -> str:
        retried = f", {self.retries} retried" if self.retries else ""
        return (f"{self.items} items in {self.seconds:.2f}s "
                f"({self.items_per_second:.1f}/s, {self.requests} requests{retried}, "
                f"{self.tokens_per_item:.0f} tokens/item, ${self.usd:.5f})")


class _Limiter:
    """A plain sliding window, shared by every worker."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._recent: list[float] = []
        self._lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._recent = [t for t in self._recent if now - t < 60.0]
                if len(self._recent) < self.per_minute:
                    self._recent.append(now)
                    return
                wait = 60.0 - (now - self._recent[0])
            time.sleep(max(0.01, wait))


class Client:
    """A thin client that packs, parallelises, dedupes and retries."""

    def __init__(
        self,
        key: str | None = None,
        *,
        url: str = URL,
        model: str = MODEL,
        pack: int = PACK,
        workers: int = WORKERS,
        requests_per_minute: int = REQUESTS_PER_MINUTE,
        cache: bool = True,
        transport: str = "auto",
    ) -> None:
        self.key = key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.key:
            raise JevError("no key: pass one, or set TYPESAFE_API_KEY")
        self.url = url
        self.model = model
        self.pack = max(1, pack)
        self.workers = max(1, workers)
        self.cache_on = cache
        if transport not in ("auto", "http2", "threads"):
            raise JevError('transport must be "auto", "http2" or "threads"')
        if transport == "http2" and not _http2.available():
            raise JevError('transport="http2" needs httpx and h2: '
                           'pip install "jev-ultralightspeed[fast]"')
        if transport == "http2" and _http2.in_a_loop():
            raise JevError('transport="http2" cannot run inside an event loop: call this from a '
                           'thread (asyncio.to_thread), or pass transport="threads"')
        self.transport = ("http2" if transport == "auto" and _http2.available()
                          else "threads" if transport == "auto" else transport)
        self._cache: OrderedDict[tuple, Answer] = OrderedDict()
        self._local = threading.local()
        self._pipe = None
        self._pool: ThreadPoolExecutor | None = None
        self._books = threading.Lock()
        self.latencies: list[float] = []
        self._limiter = _Limiter(requests_per_minute)
        self.usage = Usage()

    # -- the one call ------------------------------------------------------
    def _connection(self) -> http.client.HTTPSConnection:
        """One live connection per worker thread, kept between requests."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        parts = urllib.parse.urlsplit(self.url)
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=TIMEOUT_S,
            context=ssl.create_default_context())
        self._local.connection = connection
        return connection

    def _drop_connection(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
            self._local.connection = None

    def ask(self, body: dict) -> dict:
        """
        One request, on a connection this thread already has open, retrying
        the statuses worth retrying. A dropped connection is reopened once
        without counting as a failure: keeping it alive is the whole point.
        """
        data = json.dumps(body).encode("utf-8")
        path = urllib.parse.urlsplit(self.url).path or "/"
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {self.key}",
                   "connection": "keep-alive",
                   "accept": "application/json"}
        for attempt in range(MAX_RETRIES):
            self._limiter.take()
            started = time.monotonic()
            try:
                connection = self._connection()
                connection.request("POST", path, body=data, headers=headers)
                answer = connection.getresponse()
                payload = answer.read()
                self.latencies.append((time.monotonic() - started) * 1000)
                if answer.status == 200:
                    return json.loads(payload.decode("utf-8"))
                self._drop_connection()
                if answer.status not in RETRY_STATUSES or attempt == MAX_RETRIES - 1:
                    raise JevError(f"Jev answered {answer.status}: "
                                   f"{payload.decode('utf-8', 'replace')[:300]}")
                self.usage.retries += 1
            except (http.client.HTTPException, socket.error, ssl.SSLError, TimeoutError) as error:
                self._drop_connection()
                if attempt == MAX_RETRIES - 1:
                    raise JevError(f"could not reach Jev: {error}") from error
                self.usage.retries += 1
            time.sleep(min(30.0, 2 ** attempt))
        raise JevError("out of retries")

    def _pipe_for(self) -> "_http2.Pipe":
        if self._pipe is None:
            self._pipe = _http2.Pipe(self.url, self.key, inflight=self.workers,
                                     timeout=TIMEOUT_S, retry_statuses=RETRY_STATUSES,
                                     max_retries=MAX_RETRIES,
                                     requests_per_minute=self._limiter.per_minute)
        return self._pipe

    def _pool_for(self) -> ThreadPoolExecutor:
        """
        One pool for the life of the client. The connections live in thread
        local storage, so a pool per call would throw away every connection it
        had just opened, warming included.
        """
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.workers,
                                            thread_name_prefix="jev")
        return self._pool

    def warm(self, workers: int | None = None) -> None:
        """
        Open the connections before the work arrives, so the first items do
        not pay for a TLS handshake. Harmless to call twice.
        """
        if self.transport == "http2" and not _http2.in_a_loop():
            self._pipe_for().warm()
            return
        if self.transport == "http2":
            return              # inside a loop the threaded path runs; nothing to warm
        pool = self._pool_for()
        # One task per worker, held until all of them have a connection, so
        # the work is not all done by the first thread to wake up.
        gate = threading.Barrier(self.workers, timeout=TIMEOUT_S)

        def open_one(_):
            self._connection()
            try:
                gate.wait()
            except threading.BrokenBarrierError:      # a worker gave up; not fatal
                pass

        list(pool.map(open_one, range(self.workers)))

    # -- the useful call ---------------------------------------------------
    def close(self) -> None:
        """Let go of every connection this client opened."""
        self._drop_connection()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None

    def stream(
        self,
        items: Iterable[str],
        instructions: str,
        *,
        criteria: dict | None = None,
        options: dict | None = None,
        chunk: int = 5_000,
    ):
        """
        The same work, a chunk at a time, so a million rows cost the memory
        of one chunk rather than of the job. Each chunk is classified in full
        and then yielded in order: answers do not arrive one by one the moment
        each request lands, and the last item of a chunk waits for the rest of
        that chunk.

            for answer in client.stream(rows, question):
                writer.writerow([answer.item, answer.label, answer.p])

        The items are taken from any iterable, so they can come off a cursor
        or a file without being read into a list first.
        """
        batch: list[str] = []
        for item in items:
            batch.append(item)
            if len(batch) >= chunk:
                yield from self.classify(batch, instructions, criteria=criteria, options=options)
                batch = []
        if batch:
            yield from self.classify(batch, instructions, criteria=criteria, options=options)

    def classify(
        self,
        items: Sequence[str],
        instructions: str,
        *,
        criteria: dict | None = None,
        options: dict | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Answer]:
        """
        Answer one question about every item, in order.

        `criteria` says what true and false mean for a yes/no question;
        `options` turns it into a pick-one question over those names.
        """
        texts = [_clean(item) for item in items]
        if not texts:
            return []

        answers: list[Answer | None] = [None] * len(texts)
        todo: list[int] = []
        for index, text in enumerate(texts):
            hit = self._cached(text, instructions, criteria, options)
            if hit is not None:
                answers[index] = _copy_answer(hit, text)
                self.usage.cached += 1
            else:
                todo.append(index)

        # The same text twice is one question, answered once.
        first_seen: dict[str, int] = {}
        unique: list[int] = []
        duplicates: dict[int, int] = {}
        for index in todo:
            text = texts[index]
            if text in first_seen:
                duplicates[index] = first_seen[text]
                self.usage.cached += 1
            else:
                first_seen[text] = index
                unique.append(index)

        groups = [unique[at:at + self.pack] for at in range(0, len(unique), self.pack)]
        done = 0
        started = time.monotonic()
        # Inside somebody else's event loop the threaded path is used instead.
        if groups and self.transport == "http2" and not _http2.in_a_loop():
            # One connection, every request in flight on it.
            bodies = [self._body([texts[i] for i in g], instructions, criteria, options)
                      for g in groups]
            payloads = self._pipe_for().ask_all(
                bodies, on_request=self._count, on_timing=self.latencies.append,
                on_retry=self._count_retry)
            for group, data in zip(groups, payloads):
                for position, index in enumerate(group, start=1):
                    answer = _read(self._entry(data, position), texts[index])
                    answers[index] = answer
                    self._remember(texts[index], instructions, criteria, options, answer)
                done += len(group)
                if on_progress:
                    on_progress(done, len(unique))
        elif groups:
            pool = self._pool_for()
            for group, result in zip(groups, pool.map(
                lambda g: self._ask_group([texts[i] for i in g], instructions, criteria, options),
                groups,
            )):
                for index, answer in zip(group, result):
                    answers[index] = answer
                    self._remember(texts[index], instructions, criteria, options, answer)
                done += len(group)
                if on_progress:
                    on_progress(done, len(unique))
        for index, source in duplicates.items():
            answers[index] = _copy_answer(answers[source], texts[index])

        missing = [index for index, answer in enumerate(answers) if answer is None]
        if missing:
            raise JevError(f"{len(missing)} of {len(texts)} items came back without an answer; "
                           f"the first is item {missing[0] + 1}")
        self.usage.items += len(texts)
        self.usage.seconds += time.monotonic() - started
        return answers

    # -- the parts ---------------------------------------------------------
    def _body(self, texts: Sequence[str], instructions: str,
              criteria: dict | None, options: dict | None) -> dict:
        return (_one_body(self.model, texts[0], instructions, criteria, options)
                if len(texts) == 1
                else _packed_body(self.model, texts, instructions, criteria, options))

    def _entry(self, data: dict, position: int) -> dict:
        entry = (data.get("answers") or {}).get(f"item_{position}")
        if entry is None:
            raise JevError(f"Jev did not answer item_{position} of a packed request")
        return entry

    def _ask_group(self, texts: Sequence[str], instructions: str,
                   criteria: dict | None, options: dict | None) -> list[Answer]:
        data = self.ask(self._body(texts, instructions, criteria, options))
        self._count(data)
        return [_read(self._entry(data, position), text)
                for position, text in enumerate(texts, start=1)]

    def _count(self, data: dict) -> None:
        usage = data.get("usage") or {}
        with self._books:                       # several threads add to these
            self.usage.requests += 1
            self.usage.input_tokens += int(usage.get("input_tokens") or 0)
            self.usage.output_tokens += int(usage.get("output_tokens") or 0)

    def _count_retry(self) -> None:
        with self._books:
            self.usage.retries += 1

    def _key(self, text: str, instructions: str, criteria, options) -> tuple:
        return (self.model, text, instructions, json.dumps(criteria, sort_keys=True),
                json.dumps(options, sort_keys=True))

    def _cached(self, text, instructions, criteria, options) -> Answer | None:
        if not self.cache_on:
            return None
        return self._cache.get(self._key(text, instructions, criteria, options))

    def _remember(self, text, instructions, criteria, options, answer: Answer) -> None:
        if not self.cache_on:
            return
        # A copy: what the caller was handed is theirs to mutate.
        self._cache[self._key(text, instructions, criteria, options)] = _copy_answer(answer, text)
        while len(self._cache) > 10_000:                 # bounded, oldest first
            self._cache.popitem(last=False)


# -- request shapes ---------------------------------------------------------

def _question(instructions: str, criteria: dict | None, options: dict | None) -> dict:
    if options:
        return {"type": "choice", "instructions": instructions,
                "criteria": {str(k): str(v) for k, v in options.items()}}
    question = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = {str(k): str(v) for k, v in criteria.items()}
    return question


def _one_body(model, text, instructions, criteria, options) -> dict:
    return {"model": model, "state": {"item_1": text},
            "questions": {"item_1": _question(instructions, criteria, options)}}


def _packed_body(model, texts: Sequence[str], instructions, criteria, options) -> dict:
    """
    Several items in one state, one question each, every question naming the
    item it is about. Nothing about the question changes but that name.
    """
    state = {f"item_{position}": text for position, text in enumerate(texts, start=1)}
    questions = {}
    for position in range(1, len(texts) + 1):
        name = f"item_{position}"
        question = _question(
            f"{instructions} Judge {name} only, ignoring every other item.",
            criteria, options)
        questions[name] = question
    return {"model": model, "state": state, "questions": questions}


def _copy_answer(answer: "Answer", text: str) -> "Answer":
    """The same answer for another copy of the text, sharing nothing mutable."""
    return Answer(item=text, p=answer.p, label=answer.label, kind=answer.kind,
                  distribution=dict(answer.distribution), confidence=answer.confidence)


def _read(entry: dict, text: str) -> Answer:
    if "noul" in entry:
        p = float(entry["noul"])
        return Answer(item=text, p=p, label="yes" if p >= 0.5 else "no", kind="noul",
                      distribution={"yes": p, "no": 1.0 - p},
                      confidence=_float_or_none(entry.get("confidence")))
    if "choice" in entry:
        distribution = {str(k): float(v) for k, v in (entry.get("probabilities") or {}).items()}
        label = str(entry["choice"])
        return Answer(item=text, p=distribution.get(label, 0.0), label=label, kind="choice",
                      distribution=distribution,
                      confidence=_float_or_none(entry.get("confidence")))
    raise JevError(f"unreadable answer: {list(entry)[:5]}")


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(item: str) -> str:
    text = item if isinstance(item, str) else str(item)
    if len(text) > MAX_ITEM_CHARS:
        raise JevError(f"item over {MAX_ITEM_CHARS:,} characters; split it first")
    return text


# -- the short way ----------------------------------------------------------

def classify(items: Iterable[str], instructions: str, **kwargs) -> list[Answer]:
    """One call for the common case. Keyword arguments go to `Client`."""
    client_arguments = {name: kwargs.pop(name) for name in
                        ("key", "url", "model", "pack", "workers", "requests_per_minute",
                         "cache", "transport")
                        if name in kwargs}
    client = Client(**client_arguments)
    try:
        return client.classify(list(items), instructions, **kwargs)
    finally:
        client.close()
