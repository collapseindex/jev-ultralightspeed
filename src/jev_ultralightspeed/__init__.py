"""
jev-ultralightspeed: classify a pile of items with Jev, fast.

The naive loop sends one item per request and waits for each answer, which
spends most of its time on round trips and re-sends the same overhead every
time. Three changes, none of them clever, together give roughly twenty times
the throughput for a little over a third of the tokens:

  pack        several items into one request, one question each
  parallel    several requests in flight, under the published rate limit
  dedupe      identical text asked once

Everything here is standard library. Bring your own key.

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

__version__ = "0.1.0"

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
    requests: int = 0
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
        return (f"{self.items} items in {self.seconds:.2f}s "
                f"({self.items_per_second:.1f}/s, {self.requests} requests, "
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
    ) -> None:
        self.key = key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.key:
            raise JevError("no key: pass one, or set TYPESAFE_API_KEY")
        self.url = url
        self.model = model
        self.pack = max(1, pack)
        self.workers = max(1, workers)
        self.cache_on = cache
        self._cache: OrderedDict[tuple, Answer] = OrderedDict()
        self._local = threading.local()
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
            try:
                connection = self._connection()
                connection.request("POST", path, body=data, headers=headers)
                answer = connection.getresponse()
                payload = answer.read()
                if answer.status == 200:
                    return json.loads(payload.decode("utf-8"))
                self._drop_connection()
                if answer.status not in RETRY_STATUSES or attempt == MAX_RETRIES - 1:
                    raise JevError(f"Jev answered {answer.status}: "
                                   f"{payload.decode('utf-8', 'replace')[:300]}")
            except (http.client.HTTPException, socket.error, ssl.SSLError, TimeoutError) as error:
                self._drop_connection()
                if attempt == MAX_RETRIES - 1:
                    raise JevError(f"could not reach Jev: {error}") from error
            time.sleep(min(30.0, 2 ** attempt))
        raise JevError("out of retries")

    def warm(self, workers: int | None = None) -> None:
        """
        Open the connections before the work arrives, so the first items do
        not pay for a TLS handshake. Harmless to call twice.
        """
        count = workers or self.workers
        def open_one(_):
            self._connection()
        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(open_one, range(count)))

    # -- the useful call ---------------------------------------------------
    def close(self) -> None:
        """Let go of this thread's connection. Workers drop theirs with the pool."""
        self._drop_connection()

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
                answers[index] = Answer(**{**hit.__dict__, "item": text})
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
        if groups:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
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
            answers[index] = Answer(**{**answers[source].__dict__, "item": texts[index]})

        self.usage.items += len(texts)
        self.usage.seconds += time.monotonic() - started
        return [answer for answer in answers if answer is not None]

    # -- the parts ---------------------------------------------------------
    def _ask_group(self, texts: Sequence[str], instructions: str,
                   criteria: dict | None, options: dict | None) -> list[Answer]:
        if len(texts) == 1:
            body = _one_body(self.model, texts[0], instructions, criteria, options)
            data = self.ask(body)
            self._count(data)
            return [_read(data["answers"]["item_1"], texts[0])]
        body = _packed_body(self.model, texts, instructions, criteria, options)
        data = self.ask(body)
        self._count(data)
        answers = data.get("answers") or {}
        out = []
        for position, text in enumerate(texts, start=1):
            entry = answers.get(f"item_{position}")
            if entry is None:
                raise JevError(f"Jev did not answer item_{position} of a packed request")
            out.append(_read(entry, text))
        return out

    def _count(self, data: dict) -> None:
        usage = data.get("usage") or {}
        self.usage.requests += 1
        self.usage.input_tokens += int(usage.get("input_tokens") or 0)
        self.usage.output_tokens += int(usage.get("output_tokens") or 0)

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
        self._cache[self._key(text, instructions, criteria, options)] = answer
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
                        ("key", "url", "model", "pack", "workers", "requests_per_minute", "cache")
                        if name in kwargs}
    client = Client(**client_arguments)
    return client.classify(list(items), instructions, **kwargs)
