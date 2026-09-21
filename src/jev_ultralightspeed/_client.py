"""The client, and the two calls most people want."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for
from typing import Callable, Iterable, Sequence

from . import _http2
from ._answers import Answer, Ask, Usage, _Ask, _copy_answer, _read
from ._errors import JevError
from ._ledger import Ledger
from ._limits import _Limiter
from ._protocol import _clean, _one_body, _packed_body, _question
from ._settings import (CHARS_PER_TOKEN, DRAIN_S, MAX_FAILURES_KEPT,
                        MAX_LATENCIES, MAX_REQUEST_TOKENS, MAX_RETRIES, MAX_STATE_TOKENS,
                        MIN_SKIP_BUDGET, MODEL, PACK, REQUESTS_PER_MINUTE, RETRY_STATUSES,
                        SKIP_FRACTION, SSL_CONTEXT, TIMEOUT_S, URL, WARM_TIMEOUT_S,
                        WINDOW_PER_WORKER, WORKERS, _CLIENT_ARGUMENTS)


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
        paced: bool = False,
        chars_per_token: float = CHARS_PER_TOKEN,
        cache: bool = True,
        dedupe: bool = True,
        transport: str = "auto",
        verify: str | os.PathLike | ssl.SSLContext | None = None,
        guidance: str = "repeat",
        limiter=None,
    ) -> None:
        self.key = key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.key:
            raise JevError("no key: pass one, or set TYPESAFE_API_KEY")
        self.url = url
        self.model = model
        self.pack = max(1, pack)
        self.workers = max(1, workers)
        # How many characters the planner assumes a token is worth. 3.5 is
        # conservative for English against 3.92 measured, and badly wrong for code,
        # CJK or anything emoji-heavy, where a token can be one character or less.
        # Lower it there, or requests go out larger than they are supposed to be.
        self.chars_per_token = max(0.25, chars_per_token)
        self.cache_on = cache
        # Asking the same text twice is usually waste. It is not waste when the
        # repeat is the measurement, so it can be turned off.
        self.dedupe = dedupe
        if guidance not in ("repeat", "once"):
            raise JevError('guidance must be "repeat" or "once"')
        # Whether the question is written into every item's question or carried
        # once in the state. "once" is a different prompt, so it is opt in and it
        # is part of the cache key.
        self.guidance = guidance
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
        # Whose word to take for the server's certificate. None is the machine's
        # own trust store, which is right for api.typesafe.ai and wrong for a
        # gateway signed by a private CA, which `url` otherwise invites you to use.
        # A path or a context, never a boolean: turning verification off is a
        # thing you should have to write out yourself.
        self.verify = verify
        self._cache: OrderedDict[tuple, Answer] = OrderedDict()
        self._local = threading.local()
        self._pipe = None
        self._pool: ThreadPoolExecutor | None = None
        self._ledgers: dict[str, Ledger] = {}
        self._books = threading.Lock()
        self.last_partial: list = []       # answers that arrived before a failed run gave up
        self.failures: list[Answer] = []   # items skipped under on_error="skip"
        self.latencies: list[float] = []
        self.http_version = ""             # what the connection actually negotiated
        # A ceiling of its own unless it is handed one. Anything with `take()` and
        # `try_take()` will do, which is how several clients hold one budget between
        # them: two in one process for a fair benchmark, or a Redis window shared by
        # machines. This library does not ship the second kind, it just gets out of
        # the way of it.
        self._limiter = limiter if limiter is not None else _Limiter(requests_per_minute,
                                                                     paced=paced)
        self.usage = Usage()

    # -- the one call ------------------------------------------------------
    def _trust(self) -> ssl.SSLContext:
        """The context to verify the server with, built once per call site."""
        if isinstance(self.verify, SSL_CONTEXT):
            return self.verify
        if self.verify is not None:
            return ssl.create_default_context(cafile=os.fspath(self.verify))
        return ssl.create_default_context()

    def _connection(self) -> http.client.HTTPSConnection:
        """One live connection per worker thread, kept between requests."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme == "http":                  # a gateway, a proxy, or a test double
            connection = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                                    timeout=TIMEOUT_S)
        else:
            connection = http.client.HTTPSConnection(
                parts.hostname, parts.port or 443, timeout=TIMEOUT_S,
                context=self._trust())
            self.http_version = "HTTP/1.1"      # the standard library speaks one protocol
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
        the statuses worth retrying. A dropped connection is reopened and the
        attempt counted as a retry, like any other.
        """
        data = json.dumps(body).encode("utf-8")
        path = urllib.parse.urlsplit(self.url).path or "/"
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {self.key}",
                   "connection": "keep-alive",
                   "accept": "application/json"}
        for attempt in range(MAX_RETRIES):
            wait = 0.0
            self._limiter.take()
            started = time.monotonic()
            try:
                connection = self._connection()
                connection.request("POST", path, body=data, headers=headers)
                answer = connection.getresponse()
                payload = answer.read()
                self._note_latency((time.monotonic() - started) * 1000)
                if answer.status == 200:
                    return json.loads(payload.decode("utf-8"))
                hint = answer.getheader("retry-after")
                self._drop_connection()
                if answer.status not in RETRY_STATUSES or attempt == MAX_RETRIES - 1:
                    raise JevError(f"Jev answered {answer.status}: "
                                   f"{payload.decode('utf-8', 'replace')[:300]}")
                wait = _http2._backoff(attempt, hint)
                self._count_retry(answer.status, wait)
            except (http.client.HTTPException, socket.error, ssl.SSLError, TimeoutError) as error:
                self._drop_connection()
                if attempt == MAX_RETRIES - 1:
                    raise JevError(f"could not reach Jev: {error}") from error
                wait = _http2._backoff(attempt, None)
                self._count_retry(0, wait)
            time.sleep(wait)                    # jittered, and the server's hint when it gave one
        raise JevError("out of retries")

    def _pipe_for(self) -> "_http2.Pipe":
        if self._pipe is None:
            self._pipe = _http2.Pipe(self.url, self.key, inflight=self.workers,
                                     timeout=TIMEOUT_S, retry_statuses=RETRY_STATUSES,
                                     max_retries=MAX_RETRIES, limiter=self._limiter,
                                     verify=self._trust(),
                                     on_protocol=self._note_protocol)
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
        gate = threading.Barrier(self.workers, timeout=WARM_TIMEOUT_S)

        def open_one(_):
            connection = self._connection()
            # Constructing an HTTPConnection opens nothing: it connects on the
            # first request. Warming that skips this measured zero handshakes.
            connection.timeout = WARM_TIMEOUT_S       # a warm-up is never a wait worth minutes
            try:
                connection.connect()
            except OSError:
                pass                                  # warming is an optimisation, not a step
            finally:
                connection.timeout = TIMEOUT_S
            try:
                gate.wait()
            except threading.BrokenBarrierError:      # a worker gave up; not fatal
                pass

        list(pool.map(open_one, range(self.workers)))

    # -- the useful call ---------------------------------------------------
    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        """Let go of every connection this client opened, and every file."""
        for ledger in self._ledgers.values():
            ledger.close()
        self._ledgers.clear()
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
        levels: Sequence[str] | None = None,
        chunk: int = 5_000,
        checkpoint: str | os.PathLike | None = None,
        on_error: str = "raise",
        on_progress: Callable[[int, int], None] | None = None,
    ):
        """
        The same work, answers handed back as the requests they ride on land.

            for answer in client.stream(rows, question):
                writer.writerow([answer.item, answer.label, answer.p])

        `chunk` is how many items are held in memory and how far deduplication
        looks, not a barrier: within a chunk the requests roll, and an answer is
        yielded as soon as everything before it is known. Only a repeat whose
        original is still in flight has to wait, and it waits for that one
        request rather than for the whole chunk.

        The items are taken from any iterable, so they can come off a cursor
        or a file without being read into a list first.

        With `checkpoint` set, every answer is appended to that file as its
        request lands and a rerun skips whatever is already in it, which is how
        a job of a million rows survives being killed at row 800,000.
        """
        batch: list[str] = []
        for item in items:
            batch.append(item)
            if len(batch) >= chunk:
                yield from self._answers_for(self._asks(batch, instructions, criteria,
                                                       options, levels),
                                             on_progress, checkpoint, on_error)
                batch = []
        if batch:
            yield from self._answers_for(self._asks(batch, instructions, criteria, options,
                                                   levels),
                                         on_progress, checkpoint, on_error)

    def classify(
        self,
        items: Sequence[str],
        instructions: str,
        *,
        criteria: dict | None = None,
        options: dict | None = None,
        levels: Sequence[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        checkpoint: str | os.PathLike | None = None,
        on_error: str = "raise",
    ) -> list[Answer]:
        """
        Answer one question about every item, in order.

        `criteria` says what true and false mean for a yes/no question;
        `options` turns it into a pick-one question over those names; `levels`
        turns it into a score, an ordered list from low to high, and the answer
        lands somewhere along it rather than on one of them.

        `on_progress(done, total)` is called as each request lands, which on the
        fast transport means from a background thread. Keep it cheap, and lock
        anything it touches.

        `checkpoint` is a file of answers that survives the process. Anything
        already in it is not asked again, and every new answer is appended as
        its request lands, so the same call run twice costs nothing the second
        time and a killed run resumes where it stopped.

        `on_error="skip"` finishes the job around the rows it cannot do. An item
        whose answer will not read, and a request that fails for good, leave an
        `Answer` with `ok` False and `error` set, in place, and the run carries
        on. They are listed in `client.failures` and counted in `usage.skipped`,
        and they are **not** written to the checkpoint, so a rerun tries them
        again. Up to one percent of the items in the call may go this way, or
        five requests' worth, whichever is larger; past that the run is
        abandoned and raises, because a wrong key must not be skipped a million
        times over.
        """
        return list(self._answers_for(self._asks(items, instructions, criteria, options, levels),
                                      on_progress, checkpoint, on_error))

    def judge(
        self,
        asks: Iterable[Ask],
        *,
        instructions: str | None = None,
        criteria: dict | None = None,
        options: dict | None = None,
        levels: Sequence[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        checkpoint: str | os.PathLike | None = None,
        on_error: str = "raise",
    ) -> list[Answer]:
        """
        Answer a different question about each item, packed the same way.

            answers = client.judge([
                Ask(row.text, options=row.allowed_labels)
                for row in rows
            ], instructions="Which of these applies?")

        The API has always had one question per item; `classify` is the case
        where they all happen to be the same. This is the case where they are
        not: an answer space that differs per row cannot be grouped by question
        at all, so without this it is a pack of one every time.

        Anything an `Ask` leaves out falls back to what this call was given.
        Packing, deduplication, the cache, the checkpoint and `triage` all work
        as they do for `classify`, except that two rows count as duplicates only
        when the question matches as well as the text. `guidance="once"` needs a
        pack to be asking one thing before there is anything to hoist, so a mixed
        pack quietly keeps its questions where they are.
        """
        prepared = []
        # Copied once per distinct question, not per row, so the rows that share
        # one share the object. The engine leans on that.
        copies: dict[int, tuple] = {}
        for index, ask in enumerate(asks):
            if not isinstance(ask, Ask):
                raise JevError(f"judge() takes Ask objects; item {index + 1} is a "
                               f"{type(ask).__name__}. classify() takes plain text.")
            mark = (id(ask.instructions), id(ask.criteria), id(ask.options), id(ask.levels))
            settled = copies.get(mark)
            if settled is None:
                words = ask.instructions if ask.instructions is not None else instructions
                if not words:
                    raise JevError(f"item {index + 1} has no instructions, and none were "
                                   f"given to judge() either")
                these = ask.criteria if ask.criteria is not None else criteria
                those = ask.options if ask.options is not None else options
                steps = ask.levels if ask.levels is not None else levels
                if those and steps:
                    raise JevError(f"item {index + 1} has both options and levels; a question "
                                   f"picks one of a set or scores along a list, not both")
                settled = copies[mark] = (words, dict(these) if these else these,
                                          dict(those) if those else those,
                                          tuple(steps) if steps else steps)
            prepared.append(_Ask(_clean(ask.item, index), *settled))
        return list(self._answers_for(prepared, on_progress, checkpoint, on_error))

    def _asks(self, items, instructions, criteria, options, levels=None) -> list["_Ask"]:
        """
        The same question against every item, which is what `classify` means.

        The mappings are copied once here rather than per row: a stream is
        suspended between answers and the caller still holds them, so changing
        one mid-run would otherwise change the questions still to be sent.
        """
        if options and levels:
            raise JevError("a question picks one of a set or scores along a list, not both")
        criteria = dict(criteria) if criteria else criteria
        options = dict(options) if options else options
        levels = tuple(levels) if levels else levels
        return [_Ask(_clean(item, index), instructions, criteria, options, levels)
                for index, item in enumerate(items)]

    def _answers_for(self, asks, on_progress, checkpoint, on_error):
        """
        The engine both callers share: plan the requests, keep a bounded number
        of them in flight, bank each one where it lands, and give answers back in
        the order they were asked for.

        Banking happens in completion order and yielding in input order, on
        purpose. Durability should not wait behind a straggler, and the caller's
        order is the promise.
        """
        if on_error not in ("raise", "skip"):
            raise JevError('on_error must be "raise" or "skip"')
        asks = list(asks)
        if not asks:
            return
        texts = [ask.text for ask in asks]

        # One shape and one overhead per distinct question, not per item. Rows
        # sharing a question share the object, because the questions were copied
        # once on the way in, so identity finds them without comparing dicts.
        shapes: list[tuple] = []
        overheads: list[int] = []
        worked_out: dict[tuple, tuple] = {}
        for ask in asks:
            mark = (id(ask.instructions), id(ask.criteria), id(ask.options), id(ask.levels))
            known = worked_out.get(mark)
            if known is None:
                known = worked_out[mark] = (self._shape(ask.instructions, ask.criteria,
                                                        ask.options, ask.levels),
                                            self._overhead(ask))
            shapes.append(known[0])
            overheads.append(known[1])

        answers: list[Answer | None] = [None] * len(texts)
        self.last_partial = []
        ledger = self._ledger_for(checkpoint)
        todo: list[int] = []
        for index, text in enumerate(texts):
            hit = self._cached(text, shapes[index])
            if hit is not None:
                answers[index] = _copy_answer(hit, text)
                self.usage.cached += 1
                continue
            stored = (ledger.answer_for(self._key(text, shapes[index]), text)
                      if ledger is not None else None)
            if stored is not None:
                answers[index] = Answer(item=text, p=stored.p, label=stored.label,
                                        kind=stored.kind, distribution=dict(stored.distribution),
                                        confidence=stored.confidence,
                                        position=stored.position, packed=stored.packed,
                                        score=stored.score)
                self.usage.resumed += 1
            else:
                todo.append(index)

        # The same text twice is one question, answered once.
        first_seen: dict[tuple, int] = {}
        unique: list[int] = []
        copies: dict[int, list[int]] = {}
        for index in todo:
            # Keyed by the question as well as the text: with judge() the same
            # row can be asked two different things, and those are two answers.
            text = (texts[index], shapes[index])
            if self.dedupe and text in first_seen:
                copies.setdefault(first_seen[text], []).append(index)
                self.usage.cached += 1
            else:
                first_seen[text] = index
                unique.append(index)

        groups = self._plan(unique, asks, overheads)
        # One budget in items, whether they were lost an item or a request at a
        # time, so "skip" cannot turn a broken run into a million empty answers.
        budget = (max(MIN_SKIP_BUDGET * self.pack, int(len(unique) * SKIP_FRACTION))
                  if on_error == "skip" else 0)
        given_up = [0]

        def spare(count: int) -> bool:
            """True when the run can afford to lose this many items and carry on."""
            if on_error != "skip":
                return False
            with self._books:
                given_up[0] += count
                return given_up[0] <= budget

        def settle(index: int, answer: Answer) -> None:
            answers[index] = answer
            if answer.ok:
                self._remember(texts[index], shapes[index], answer)
            for copy in copies.get(index, ()):
                answers[copy] = _copy_answer(answer, texts[copy])
                if not answer.ok:
                    # Counted as cached when it was set aside, before there was
                    # an answer to look at. A copy of nothing is not goodput.
                    with self._books:
                        self.usage.cached -= 1
                        self.usage.skipped += 1

        rolling = self.transport == "http2" and not _http2.in_a_loop() and bool(groups)
        pipe = self._pipe_for() if rolling else None
        run = pipe.new_run() if rolling else None
        pool = None if rolling else (self._pool_for() if groups else None)

        def start(which: int):
            here = [asks[index] for index in groups[which]]
            if rolling:
                return pipe.submit(self._body(here), run, on_request=self._count,
                                   on_timing=self._note_latency, on_retry=self._count_retry)
            return pool.submit(self._ask_group, here, spare)

        def finish(which: int, future) -> list[Answer]:
            if rolling:
                return self._read_all(future.result(),
                                      [texts[index] for index in groups[which]], spare)
            return future.result()

        cursor = 0
        done = 0
        started = time.monotonic()
        queued = deque(range(len(groups)))
        window = max(1, self.workers * WINDOW_PER_WORKER)
        flying: dict = {}

        def fill() -> None:
            while queued and len(flying) < window:
                which = queued.popleft()
                flying[start(which)] = which

        try:
            fill()
            while flying:
                ready, _ = wait_for(list(flying), return_when=FIRST_COMPLETED)
                for future in ready:
                    which = flying.pop(future)
                    group = groups[which]
                    try:
                        here = finish(which, future)
                    except JevError as error:
                        if not spare(len(group)):
                            raise
                        here = [self._gave_up(texts[index], str(error), position, len(group))
                                for position, index in enumerate(group, start=1)]
                    self._bank(ledger, group, texts, here, shapes)
                    for index, answer in zip(group, here):
                        settle(index, answer)
                    done += len(here)
                    if on_progress:
                        on_progress(done, len(unique))
                fill()
                while cursor < len(answers) and answers[cursor] is not None:
                    yield answers[cursor]
                    cursor += 1

            while cursor < len(answers) and answers[cursor] is not None:
                yield answers[cursor]
                cursor += 1
            missing = [index for index, answer in enumerate(answers) if answer is None]
            if missing:
                raise JevError(f"{len(missing)} of {len(texts)} items came back without an "
                               f"answer; the first is item {missing[0] + 1}")
        except JevError:
            # Nothing else goes out, nothing unstarted is waited on, and what did
            # land is kept. The fast path and the threaded one both flush, or a
            # checkpoint would mean different things on each.
            queued.clear()
            if run is not None:
                # Told, not cancelled. Cancelling a request that is already in
                # the air leaves its task stuck in "cancelling" and the loop is
                # then stopped with a pending future behind it, which hangs about
                # one run in three. Anything that has not sent sees this and
                # raises; anything that has is given a moment to come back.
                if not run.broken:
                    run.broken = "the run was abandoned"
                if flying:
                    wait_for(list(flying), timeout=DRAIN_S)
            else:
                for future in flying:
                    future.cancel()          # threads: only the ones not started yet
            self.last_partial = [answer for answer in answers if answer is not None]
            if ledger is not None:
                ledger.flush()
            raise
        finally:
            if ledger is not None:
                ledger.flush()
            self.usage.items += len(texts)
            self.usage.seconds += time.monotonic() - started

    # -- the parts ---------------------------------------------------------
    def _body(self, asks: Sequence["_Ask"]) -> dict:
        return (_one_body(self.model, asks[0]) if len(asks) == 1
                else _packed_body(self.model, asks, self.guidance))

    def _entry(self, data: dict, position: int) -> dict:
        entry = (data.get("answers") or {}).get(f"item_{position}")
        if entry is None:
            raise JevError(f"Jev did not answer item_{position} of a packed request")
        return entry

    def _ask_group(self, asks: Sequence["_Ask"], spare=None) -> list[Answer]:
        data = self.ask(self._body(asks))
        self._count(data)
        return self._read_all(data, [ask.text for ask in asks], spare)

    def _read_all(self, data: dict, texts: Sequence[str], spare=None) -> list[Answer]:
        """
        One payload's answers, in order, read one item at a time.

        Without `spare` an unreadable item raises, which is right for a call you
        are watching. With it, that item alone is given up on: one row whose
        answer cannot be read must not be able to end a job of a million,
        because with a checkpoint the rerun reaches the same row and dies in the
        same place, forever.
        """
        here: list[Answer] = []
        for position, text in enumerate(texts, start=1):
            try:
                here.append(_read(self._entry(data, position), text, position, len(texts)))
            except JevError as error:
                if spare is None or not spare(1):
                    raise
                here.append(self._gave_up(text, str(error), position, len(texts)))
        return here

    def _gave_up(self, text: str, why: str, position: int = 1, packed: int = 1) -> Answer:
        """An item with no answer, kept in place so the order still means something."""
        answer = Answer(item=text, p=0.0, label="", kind="error",
                        position=position, packed=packed, error=why)
        with self._books:
            self.usage.skipped += 1
            if len(self.failures) < MAX_FAILURES_KEPT:
                self.failures.append(answer)
        return answer

    def _count(self, data: dict) -> None:
        usage = data.get("usage") or {}
        with self._books:                       # several threads add to these
            self.usage.requests += 1
            self.usage.input_tokens += int(usage.get("input_tokens") or 0)
            self.usage.output_tokens += int(usage.get("output_tokens") or 0)

    def _note_protocol(self, version: str) -> None:
        """
        What the first response came back on. httpx falls back to HTTP/1.1
        whenever ALPN does not offer h2, quietly, and a proxy that strips ALPN
        costs you the whole reason for installing the extra. This is how you find
        out: `client.http_version` after the first call.
        """
        if not self.http_version:
            self.http_version = version

    def _note_latency(self, milliseconds: float) -> None:
        """The last few thousand, so a long-lived client does not grow a list forever."""
        with self._books:
            self.latencies.append(milliseconds)
            if len(self.latencies) > MAX_LATENCIES:
                del self.latencies[:len(self.latencies) - MAX_LATENCIES]

    def _overhead(self, ask: "_Ask") -> int:
        """Characters one item's question adds to a request, before its text."""
        if self.guidance == "once":
            return len(json.dumps(_question("Judge item_00 only, ignoring every other item, "
                                            "against the question in guidance.",
                                            None, ask.options, ask.levels)))
        return len(json.dumps(_question(ask.instructions, ask.criteria, ask.options, ask.levels)))

    def _plan(self, indexes, asks, overheads) -> list[list[int]]:
        """
        Groups of at most `pack` items that also fit inside one request.

        `pack` counts items, and a request is refused on tokens: several
        individually legal items make an oversized one, and raising `pack` makes
        that likelier. The estimate is deliberately pessimistic, at 3.5
        characters per token against 3.92 measured on a live packed request, and
        it counts the question block once per item because that is how the API
        is shaped. With `judge()` the questions differ, so each item brings its
        own.
        """
        groups: list[list[int]] = []
        current: list[int] = []
        state_chars = 0
        request_chars = 0
        for index in indexes:
            overhead = overheads[index]
            chars = len(asks[index].text) + 12                # the item_N key rides along
            fits = (len(current) < self.pack
                    and (state_chars + chars) < MAX_STATE_TOKENS * self.chars_per_token
                    and (request_chars + chars + overhead)
                    < MAX_REQUEST_TOKENS * self.chars_per_token)
            if current and not fits:
                groups.append(current)
                current, state_chars, request_chars = [], 0, 0
            current.append(index)
            state_chars += chars
            request_chars += chars + overhead
        if current:
            groups.append(current)
        return groups

    def _bank(self, ledger, group, texts, answers, shapes) -> None:
        """
        Put these answers in the checkpoint. A skipped item is left out on
        purpose: banking "no answer" would make the next run skip it too, and
        the row would never be done.
        """
        if ledger is None:
            return
        for index, answer in zip(group, answers):
            if answer.ok:
                ledger.record(self._key(texts[index], shapes[index]), answer)

    def _ledger_for(self, checkpoint) -> Ledger | None:
        """
        The sidecar for this path, opened once. A chunked stream calls
        `classify` many times and must not read the index on each of them.
        """
        if checkpoint is None:
            return None
        path = os.fspath(checkpoint)
        if path not in self._ledgers:
            self._ledgers[path] = Ledger(path, self.model)
        return self._ledgers[path]

    def _count_retry(self, status: int = 0, waited: float = 0.0) -> None:
        """
        One attempt that will be sent again, and why.

        A count on its own generates mysteries: the headline benchmark took 245 of
        these and the run kept no record of whether they were 429s, 529s or a
        dropped connection, so it could never be settled afterwards. Status 0 is
        anything that never got an answer.
        """
        with self._books:
            self.usage.retries += 1
            self.usage.pushback[status] = self.usage.pushback.get(status, 0) + 1
            self.usage.waited += waited

    def _shape(self, instructions: str, criteria, options, levels=None) -> tuple:
        """
        Everything an answer depends on except the item itself, serialized once.

        This used to happen inside `_key`, which is called for the cache read,
        the checkpoint read, the cache write and the checkpoint write: four
        `json.dumps` of the same criteria per item, so four million of them on a
        million rows. Worth about a third of the client's own time.

        `pack` is in here as the depth you **asked for**, not the depth the answer
        came back from, and those are different things. A call of 33 items at
        pack=32 sends one request of 32 and one of 1, and that lone answer is
        filed under the same key as the other 32. Ask about it again inside a full
        pack and the lone answer is what you get.

        That is deliberate, because placement cannot be part of the key: it is not
        a function of the input. Deduplication and cache hits change the grouping,
        so the same call made twice can put the same row in a different sized
        request. A key that depended on placement would miss almost every time and
        the cache would do nothing.

        What is being traded away is measured rather than assumed. Packed against
        one per request is **+0.01 points, 95% -0.72 to +0.75** over 1,347
        completions, and no position effect is detectable at any depth. Every
        answer carries the depth it actually came from in `Answer.packed`, so a
        caller who cares can see it, and `cache=False` with `dedupe=False` and no
        checkpoint is how `bench_packing.py` controls placement when it has to.

        `guidance` is in here for a simpler reason: a different prompt is a
        different answer, and that one is a function of the input.
        """
        return (self.model, self.pack, self.guidance, instructions,
                json.dumps(criteria, sort_keys=True), json.dumps(options, sort_keys=True),
                json.dumps(list(levels) if levels else None))

    def _key(self, text: str, shape: tuple) -> tuple:
        return (text, *shape)

    def _cached(self, text, shape) -> Answer | None:
        # Asking again is the whole point of dedupe=False, and a cache read is
        # just deduplication with a longer memory.
        if not self.cache_on or not self.dedupe:
            return None
        return self._cache.get(self._key(text, shape))

    def _remember(self, text, shape, answer: Answer) -> None:
        if not self.cache_on or not self.dedupe:
            return
        # A copy: what the caller was handed is theirs to mutate.
        self._cache[self._key(text, shape)] = _copy_answer(answer, text)
        while len(self._cache) > 10_000:                 # bounded, oldest first
            self._cache.popitem(last=False)


def judge(asks: Iterable[Ask], **kwargs) -> list[Answer]:
    """One call for a pile of items with questions of their own. See `Client.judge`."""
    client_arguments = {name: kwargs.pop(name) for name in _CLIENT_ARGUMENTS
                        if name in kwargs}
    client = Client(**client_arguments)
    try:
        return client.judge(list(asks), **kwargs)
    finally:
        client.close()


def classify(items: Iterable[str], instructions: str, **kwargs) -> list[Answer]:
    """One call for the common case. Keyword arguments go to `Client`."""
    client_arguments = {name: kwargs.pop(name) for name in _CLIENT_ARGUMENTS
                        if name in kwargs}
    client = Client(**client_arguments)
    try:
        return client.classify(list(items), instructions, **kwargs)
    finally:
        client.close()
