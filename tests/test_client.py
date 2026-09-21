"""
What the client must do without touching the network: pack, dedupe, cache,
keep the order, and read every answer shape. `ask` is replaced by a fake that
answers from the request it is given, so these run anywhere, for free.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev_ultralightspeed import (  # noqa: E402
    Answer,
    Client,
    JevError,
    _Limiter,
    _packed_body,
    _read,
    classify,
)

QUESTION = "Does this need a human today?"


def asks_for(texts, instructions, *, criteria=None, options=None):
    """The engine's own record, for the tests that build a request by hand."""
    from jev_ultralightspeed import _Ask

    return [_Ask(text, instructions, criteria, options) for text in texts]


class Fake(Client):
    """A client whose requests go to a list instead of to Jev."""

    def __init__(self, answer=lambda text: 0.9, **kwargs):
        # These tests replace `ask`, which is the threaded path; the HTTP/2
        # path carries its own requests and is covered separately.
        kwargs.setdefault("transport", "threads")
        super().__init__(key="test-key", **kwargs)
        self.sent = []
        self._answer = answer

    def ask(self, body):
        self.sent.append(body)
        answers = {}
        for name, text in body["state"].items():
            answers[name] = {"type": "noul", "noul": self._answer(text)}
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10}}


def test_packs_items_and_keeps_their_order():
    client = Fake(pack=4, workers=2)
    items = [f"message {index}" for index in range(10)]
    answers = client.classify(items, QUESTION)
    assert [answer.item for answer in answers] == items          # order survives the pool
    assert len(client.sent) == 3                                  # 4 + 4 + 2
    assert client.usage.requests == 3
    assert client.usage.items == 10


def test_a_packed_question_names_the_item_it_judges():
    body = _packed_body("jev-latest", asks_for(["first", "second"], QUESTION))
    assert body["state"] == {"item_1": "first", "item_2": "second"}
    assert "item_2 only" in body["questions"]["item_2"]["instructions"]
    assert body["questions"]["item_1"]["type"] == "noul"
    # Nothing but the item's name differs between the questions.
    first, second = (body["questions"][name]["instructions"] for name in ("item_1", "item_2"))
    assert first.replace("item_1", "item_N") == second.replace("item_2", "item_N")


def test_a_single_item_is_not_packed():
    client = Fake(pack=8)
    client.classify(["only one"], QUESTION)
    assert client.sent[0]["state"] == {"item_1": "only one"}
    assert "only" not in client.sent[0]["questions"]["item_1"]["instructions"].split("Judge")[-1]


def test_identical_text_is_asked_once():
    client = Fake(pack=8)
    answers = client.classify(["same", "same", "different", "same"], QUESTION)
    asked = [text for body in client.sent for text in body["state"].values()]
    assert sorted(asked) == ["different", "same"]
    assert [answer.item for answer in answers] == ["same", "same", "different", "same"]
    assert client.usage.cached == 2


def test_the_cache_answers_the_second_call():
    client = Fake(pack=8)
    client.classify(["a", "b"], QUESTION)
    client.classify(["a", "b"], QUESTION)
    assert len(client.sent) == 1                                  # nothing asked twice
    assert client.usage.cached == 2


def test_the_cache_is_per_question_and_per_model():
    client = Fake(pack=8)
    client.classify(["a"], QUESTION)
    client.classify(["a"], "A different question entirely?")
    assert len(client.sent) == 2
    assert client.usage.cached == 0


def test_cache_can_be_turned_off():
    client = Fake(pack=8, cache=False)
    client.classify(["a"], QUESTION)
    client.classify(["a"], QUESTION)
    assert len(client.sent) == 2


def test_answers_carry_the_verdict_and_the_distribution():
    client = Fake(answer=lambda text: 0.2 if "calm" in text else 0.95, pack=2)
    answers = client.classify(["urgent thing", "calm thing"], QUESTION)
    assert [answer.label for answer in answers] == ["yes", "no"]
    assert answers[0].yes is True and answers[1].yes is False
    assert answers[0].distribution == pytest.approx({"yes": 0.95, "no": 0.05})


def test_reads_a_choice_answer():
    answer = _read({"choice": "billing", "confidence": 0.8,
                    "probabilities": {"billing": 0.8, "technical": 0.2}}, "text")
    assert (answer.label, answer.kind, answer.p) == ("billing", "choice", 0.8)
    assert answer.confidence == 0.8


def test_an_unreadable_answer_says_so():
    with pytest.raises(JevError):
        _read({"surprise": 1}, "text")


def test_a_missing_item_in_a_packed_answer_is_an_error():
    class Forgetful(Fake):
        def ask(self, body):
            answer = super().ask(body)
            answer["answers"].pop("item_2", None)
            return answer

    with pytest.raises(JevError, match="item_2"):
        Forgetful(pack=4).classify(["a", "b", "c"], QUESTION)


def test_usage_adds_up():
    client = Fake(pack=4)
    client.classify([f"item {index}" for index in range(8)], QUESTION)
    assert client.usage.requests == 2
    assert client.usage.input_tokens == 200
    assert client.usage.tokens_per_item == pytest.approx((200 + 20) / 8)
    assert client.usage.usd == pytest.approx(200 * 0.042 / 1e6)
    assert "items in" in str(client.usage)


def test_progress_is_reported_as_it_goes():
    seen = []
    client = Fake(pack=2, workers=1)
    client.classify([f"item {index}" for index in range(6)], QUESTION,
                    on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(2, 6), (4, 6), (6, 6)]


def test_nothing_in_nothing_out():
    assert Fake().classify([], QUESTION) == []


def test_an_enormous_item_is_refused_before_it_is_sent():
    client = Fake()
    with pytest.raises(JevError, match="characters"):
        client.classify(["x" * 20_001], QUESTION)
    assert client.sent == []


def test_a_missing_key_is_refused_at_once():
    with pytest.raises(JevError, match="no key"):
        Client(key="")


def test_the_rate_limiter_holds_the_line():
    limiter = _Limiter(per_minute=2)
    started = time.monotonic()
    limiter.take()
    limiter.take()                                  # both free
    assert time.monotonic() - started < 0.5
    waited = threading.Event()

    def third():
        limiter.take()
        waited.set()

    thread = threading.Thread(target=third, daemon=True)
    thread.start()
    assert waited.wait(timeout=0.5) is False        # the third one has to wait


def test_the_short_way_is_the_long_way():
    sent = []

    class Recording(Client):
        def ask(self, body):
            sent.append(body)
            return {"answers": {name: {"type": "noul", "noul": 0.6} for name in body["state"]},
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

    original = Client.ask
    Client.ask = Recording.ask
    try:
        answers = classify(["one", "two"], QUESTION, key="test-key", pack=2,
                           transport="threads")
    finally:
        Client.ask = original
    assert [answer.label for answer in answers] == ["yes", "yes"]
    assert len(sent) == 1


def test_transport_is_chosen_once_and_checked():
    from jev_ultralightspeed import _http2

    client = Client(key="k", transport="auto")
    assert client.transport == ("http2" if _http2.available() else "threads")
    assert Client(key="k", transport="threads").transport == "threads"
    with pytest.raises(JevError, match="auto"):
        Client(key="k", transport="carrier pigeon")
    if not _http2.available():
        with pytest.raises(JevError, match="httpx"):
            Client(key="k", transport="http2")


def test_stream_hands_answers_back_in_chunks():
    client = Fake(pack=4, workers=2)
    items = [f"message {index}" for index in range(10)]
    seen = list(client.stream(iter(items), QUESTION, chunk=4))
    assert [answer.item for answer in seen] == items
    # Three chunks of at most four, each packed into one request.
    assert len(client.sent) == 3
    assert client.usage.items == 10


def test_stream_takes_any_iterable():
    client = Fake(pack=2)
    seen = list(client.stream((f"row {index}" for index in range(5)), QUESTION, chunk=2))
    assert len(seen) == 5


def test_a_missing_answer_fails_loudly_instead_of_shortening_the_list():
    class Silent(Fake):
        def ask(self, body):
            answer = super().ask(body)
            answer["answers"] = {}                  # an answer for nothing at all
            return answer

    with pytest.raises(JevError):
        Silent(pack=2).classify(["a", "b"], QUESTION)


def test_a_cached_answer_shares_nothing_mutable():
    client = Fake(pack=4)
    first = client.classify(["same", "same"], QUESTION)
    first[0].distribution["yes"] = 0.0
    assert first[1].distribution["yes"] != 0.0      # the duplicate kept its own
    again = client.classify(["same"], QUESTION)
    assert again[0].distribution["yes"] != 0.0      # and so did the cache


def test_the_threads_transport_keeps_one_pool_so_warming_survives():
    client = Fake(pack=2, workers=2)
    client.classify(["a", "b", "c", "d"], QUESTION)
    pool = client._pool
    client.classify(["e", "f"], QUESTION)
    assert client._pool is pool, "a second call must not throw away the warmed pool"
    client.close()
    assert client._pool is None


def test_the_server_hint_is_a_floor_and_never_the_whole_answer():
    """
    Every worker handed the same Retry-After would come back at the same instant,
    so the jitter goes on top of the server's number rather than underneath it.
    """
    from jev_ultralightspeed import _http2

    waits = {_http2._backoff(3, "7") for _ in range(20)}
    assert all(7.0 <= wait <= 8.0 for wait in waits), sorted(waits)[:3]
    assert len(waits) > 1, "identical waits mean the workers collide again"
    assert 0.5 <= _http2._backoff(0, "next tuesday") <= 1.5   # unreadable hint, fall through
    spread = {_http2._backoff(4, None) for _ in range(20)}
    assert len(spread) > 1
    assert all(8.0 <= wait <= 24.0 for wait in spread), sorted(spread)[:3]


def test_a_retry_after_date_is_read_as_well_as_a_count():
    """RFC 9110 allows either form. Only the count used to be understood."""
    import email.utils
    import time as clock

    from jev_ultralightspeed import _http2

    soon = email.utils.formatdate(clock.time() + 30, usegmt=True)
    assert 25.0 <= _http2._retry_after_seconds(soon) <= 31.0
    past = email.utils.formatdate(clock.time() - 500, usegmt=True)
    assert _http2._retry_after_seconds(past) == 0.0            # already allowed, do not wait
    assert _http2._retry_after_seconds("12") == 12.0
    assert _http2._retry_after_seconds("later") is None
    assert _http2._retry_after_seconds(None) is None


def test_the_permit_is_taken_where_the_request_is_sent():
    import inspect

    from jev_ultralightspeed import _http2

    source = inspect.getsource(_http2.Pipe._one)
    # The permit goes inside the slot, immediately before the send, so what the
    # limiter records is when the request went out. Taken before the slot, a
    # hundred bodies against two slots spent every permit by the third send.
    assert source.index("async with self._gate") < source.index("await self._admit(run)")
    assert source.index("await self._admit(run)") < source.index("await self._client.post")
    # The backoff still waits outside the slot, because that slot is scarce.
    assert source.index("async with self._gate") < source.index("await asyncio.sleep(wait)")
    inside = source.index("async with self._gate")
    assert source.index("if run.broken", inside) > inside
    everything = inspect.getsource(_http2.Pipe._all)
    assert "return_exceptions=True" in everything, "a sibling must not cancel the rest"


def test_an_abandoned_run_stops_waiting_as_well_as_stops_sending():
    """
    A doomed run used to stop sending and carry on waiting: one refusal, and then
    every queued coroutine still took its turn at the rate limiter before the
    call came back. With permits scarce, that is most of a minute of nothing.
    """
    from jev_ultralightspeed import _http2

    if not _http2.available():
        pytest.skip("httpx not installed")
    server = Counting()
    client = Client(key="wrong", url=server.url, pack=1, workers=2,
                    requests_per_minute=20, transport="http2")
    started = time.monotonic()
    try:
        with pytest.raises(JevError):
            client.classify([f"item {n}" for n in range(100)], QUESTION)
    finally:
        took = time.monotonic() - started
        client.close()
        server.close()
    permits = len(client._limiter._recent)
    assert permits <= 25, f"{permits} of 100 permits spent on a run that was already over"
    assert took < 10.0, f"the call took {took:.1f}s to stop waiting"


def test_one_limiter_serves_both_transports():
    client = Fake(transport="threads")
    assert client._limiter is not None
    # The pipe is handed the client's limiter rather than making its own, so a
    # client cannot hold two windows and reach twice the ceiling.
    import inspect
    source = inspect.getsource(Client._pipe_for)
    assert "limiter=self._limiter" in source


def test_the_threaded_path_retries_like_the_fast_one():
    import inspect

    source = inspect.getsource(Client.ask)
    assert "_backoff" in source, "the threaded path needs jitter and Retry-After too"
    assert 'getheader("retry-after")' in source
    assert "self._count_retry(" in source, "counting retries needs the lock"
    assert "2 ** attempt" not in source, "the bare doubling should be gone"


def test_a_client_closes_itself():
    with Fake(pack=2) as client:
        client.classify(["a", "b"], QUESTION)
        assert client._pool is not None
    assert client._pool is None


def test_every_answer_says_where_it_sat():
    client = Fake(pack=4, workers=1)
    answers = client.classify([f"item {n}" for n in range(6)], QUESTION)
    assert [a.position for a in answers] == [1, 2, 3, 4, 1, 2]
    assert [a.packed for a in answers] == [4, 4, 4, 4, 2, 2]
    alone = Fake(pack=1).classify(["only"], QUESTION)
    assert (alone[0].position, alone[0].packed) == (1, 1)


def test_deduplication_can_be_turned_off_for_measuring_consistency():
    on = Fake(pack=8)
    on.classify(["same", "same", "same"], QUESTION)
    assert len(on.sent) == 1 and sum(len(b["state"]) for b in on.sent) == 1

    off = Fake(pack=8, dedupe=False)
    off.classify(["same", "same", "same"], QUESTION)
    assert sum(len(b["state"]) for b in off.sent) == 3, "each copy has to be asked"


def test_what_arrived_before_a_failure_is_kept_on_the_threaded_path():
    class FailsLate(Fake):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.calls = 0

        def ask(self, body):
            self.calls += 1
            if self.calls > 1:
                raise JevError("the second request fell over")
            return super().ask(body)

    client = FailsLate(pack=2, workers=1)
    with pytest.raises(JevError):
        client.classify(["a", "b", "c", "d"], QUESTION)
    assert len(client.last_partial) == 2, "the first group's answers should survive"


def test_latencies_do_not_grow_forever():
    from jev_ultralightspeed import MAX_LATENCIES

    client = Fake()
    for _ in range(MAX_LATENCIES + 250):
        client._note_latency(1.0)
    assert len(client.latencies) == MAX_LATENCIES


# Every behavioural test runs on both. The fast path carries its own requests,
# so a test that only ever sees `threads` is how a whole transport goes unchecked.
both_transports = pytest.mark.parametrize("transport", ["threads", "http2"])


class Counting:
    """A server that refuses everything and counts what it was asked."""

    def __init__(self, status=401):
        import json as _json
        import threading as _threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.seen = 0
        counter = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                counter.seen += 1
                self.rfile.read(int(self.headers.get("content-length", 0)))
                body = _json.dumps({"detail": "no"}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@both_transports
def test_a_doomed_run_is_abandoned_rather_than_sent_in_full(transport):
    """
    The count is the test. A wrong key used to cost every request on the fast
    path, because `gather` starts every coroutine at once and they all passed
    the abandon check before the first failure had happened.
    """
    from jev_ultralightspeed import _http2

    if transport == "http2" and not _http2.available():
        pytest.skip("httpx not installed")
    server = Counting()
    client = Client(key="wrong", url=server.url, pack=1, workers=8, transport=transport)
    try:
        with pytest.raises(JevError):
            client.classify([f"item {n}" for n in range(200)], QUESTION)
    finally:
        client.close()
        server.close()
    assert server.seen <= 24, f"{server.seen} of 200 requests went out after the run was doomed"


class Answering:
    """
    A real server on loopback that answers properly, slowly, and can be told to
    start failing. The HTTP/2 path carries its own requests, so this is the only
    way to test it the way a caller sees it.
    """

    def __init__(self, delay=0.0, fail_after=None, drop_last=False, poison=None,
                 slow_for=None, slow_by=0.0, fail_unless=None, push_back=0, status=429):
        # Threading, and it has to be: keep-alive on a single-threaded server
        # serializes the workers, so the concurrency under test disappears and
        # the run deadlocks instead of failing.
        import json as _json
        import threading as _threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.seen = 0
        self.warmed = 0
        self.bodies = []                    # what arrived, for tests about the request
        self.lock = _threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                asked = _json.loads(raw)
                with server.lock:
                    server.seen += 1
                    number = server.seen
                    server.bodies.append(raw)
                if delay:
                    time.sleep(delay)
                carries = str(asked.get("state"))
                if slow_for is not None and slow_for in carries:
                    time.sleep(slow_by)                # the straggler, chosen by content
                if fail_unless is not None and fail_unless not in carries:
                    return self.answer(401, {"detail": "no"})
                if number <= push_back:
                    self.send_response(status)
                    body = b'{"detail":"slow down"}'
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.send_header("retry-after", "0.01")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if fail_after is not None and number > fail_after:
                    return self.answer(401, {"detail": "no"})
                names = [name for name in asked["state"] if name.startswith("item_")]
                if drop_last:
                    names = names[:-1]                  # a payload one answer short
                answers = {}
                for name in names:
                    if poison is not None and poison in str(asked["state"][name]):
                        answers[name] = {"type": "noul", "shrug": "cannot say"}   # unreadable
                    else:
                        answers[name] = {"type": "noul", "noul": 0.8}
                self.answer(200, {"model": "jev-1.13.0", "answers": answers,
                                  "usage": {"input_tokens": 10, "output_tokens": 1}})

            def do_GET(self):
                with server.lock:
                    server.warmed += 1
                self.answer(405, {"detail": "post only"})   # connecting is the point

            def answer(self, status, payload):
                body = _json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def a_real_client(url, transport, **kwargs):
    from jev_ultralightspeed import _http2

    if transport == "http2" and not _http2.available():
        pytest.skip("httpx not installed")
    return Client(key="test-key", url=url, transport=transport, **kwargs)


@both_transports
def test_progress_arrives_while_the_work_does(transport):
    """
    It used to fire on the fast path only after every request had landed, which
    is a progress bar that jumps from nothing to done. The gap is the test.
    """
    server = Answering(delay=0.08)
    client = a_real_client(server.url, transport, pack=1, workers=2)
    seen = []
    try:
        answers = client.classify([f"item {n}" for n in range(8)], QUESTION,
                                  on_progress=lambda done, total: seen.append(
                                      (done, total, time.monotonic())))
        finished = time.monotonic()
    finally:
        client.close()
        server.close()

    assert len(answers) == 8
    assert [done for done, _, _ in seen] == sorted(done for done, _, _ in seen)
    assert seen[-1][0] == seen[-1][1] == 8
    assert finished - seen[0][2] >= 0.05, "every report arrived after the work was over"


@both_transports
def test_what_arrived_before_a_failure_is_kept_as_answers(transport):
    """
    On the fast path `last_partial` used to be raw payloads with no way to tell
    which item each one answered. Both paths now hand back the same thing.
    """
    server = Answering(fail_after=6)
    client = a_real_client(server.url, transport, pack=1, workers=4)
    items = [f"item {n}" for n in range(60)]
    try:
        with pytest.raises(JevError):
            client.classify(items, QUESTION)
    finally:
        client.close()
        server.close()

    assert client.last_partial, "nothing was kept from a run that half worked"
    assert all(isinstance(answer, Answer) for answer in client.last_partial)
    assert all(answer.item in items for answer in client.last_partial)
    assert len({answer.item for answer in client.last_partial}) == len(client.last_partial)


@both_transports
def test_a_packed_run_against_a_real_server_keeps_its_order(transport):
    """The fast path, end to end, with no fake in the way."""
    server = Answering()
    client = a_real_client(server.url, transport, pack=4, workers=2)
    try:
        answers = client.classify([f"item {n}" for n in range(10)], QUESTION)
    finally:
        client.close()
        server.close()

    assert [answer.item for answer in answers] == [f"item {n}" for n in range(10)]
    assert [answer.position for answer in answers] == [1, 2, 3, 4, 1, 2, 3, 4, 1, 2]
    assert client.usage.requests == 3
    assert client.usage.input_tokens == 30


def test_warming_the_fast_path_costs_a_request_on_the_budget():
    """
    A warm-up is a real request. It used to skip the limiter, so a client that
    warmed up was already one over its own ceiling before any work arrived.
    """
    server = Answering()
    client = a_real_client(server.url, "http2", pack=1, workers=2)
    try:
        client.warm()
        assert server.warmed == 1, "nothing connected"
        assert len(client._limiter._recent) == 1, "the warm-up was not on the budget"
    finally:
        client.close()
        server.close()


def test_turning_off_dedupe_also_turns_off_the_cache():
    """
    `dedupe=False` exists so a repeat is really asked again. The cache is
    deduplication with a longer memory, so leaving it on quietly undid the
    flag across `stream()` chunks.
    """
    client = Fake(pack=4, dedupe=False)
    asked = list(client.stream(iter(["same"] * 12), QUESTION, chunk=4))
    assert len(asked) == 12
    sent = sum(len(body["state"]) for body in client.sent)
    assert sent == 12, f"only {sent} of 12 repeats were actually asked"
    assert client.usage.cached == 0


# -- the checkpoint sidecar -------------------------------------------------

def records_in(path):
    """Every record line, the header not counted."""
    return [line for line in Path(path).read_text(encoding="utf-8").splitlines()[1:] if line]


@both_transports
def test_a_killed_run_resumes_where_it_stopped(transport, tmp_path):
    """
    The point of the whole thing: a run that dies does not have to be paid for
    twice. The second server's count is the test.
    """
    book = tmp_path / "run.jsonl"
    dying = Answering(fail_after=6)
    client = a_real_client(dying.url, transport, pack=1, workers=4)
    items = [f"item {n}" for n in range(60)]
    try:
        with pytest.raises(JevError):
            client.classify(items, QUESTION, checkpoint=book)
    finally:
        client.close()
        dying.close()

    saved = len(records_in(book))
    assert 0 < saved < 60, f"{saved} answers were kept from a run that half worked"

    healthy = Answering()
    second = a_real_client(healthy.url, transport, pack=1, workers=4)
    try:
        answers = second.classify(items, QUESTION, checkpoint=book)
    finally:
        second.close()
        healthy.close()

    assert len(answers) == 60
    assert [a.item for a in answers] == items
    assert healthy.seen == 60 - saved, "the second run paid for work the first had already done"
    assert second.usage.resumed == saved


@both_transports
def test_a_finished_run_repeated_costs_nothing(transport, tmp_path):
    book = tmp_path / "run.jsonl"
    server = Answering()
    items = [f"item {n}" for n in range(12)]
    first, second = [], []
    for landing in (first, second):
        client = a_real_client(server.url, transport, pack=4, workers=2)
        try:
            landing.extend(client.classify(items, QUESTION, checkpoint=book))
        finally:
            client.close()
    asked = server.seen
    server.close()

    assert asked == 3, f"{asked} requests for a job that was already done"
    assert [(a.item, a.label, a.p) for a in first] == [(a.item, a.label, a.p) for a in second]
    assert len(records_in(book)) == 12


@both_transports
def test_a_short_payload_says_which_item_is_missing(transport):
    """
    The fast path reads payloads on the loop thread now. The error a caller sees
    has to be the same one, and say the same thing.
    """
    server = Answering(drop_last=True)
    client = a_real_client(server.url, transport, pack=4, workers=2)
    try:
        with pytest.raises(JevError, match="did not answer item_4"):
            client.classify([f"item {n}" for n in range(4)], QUESTION)
    finally:
        client.close()
        server.close()


def test_a_checkpoint_is_per_question(tmp_path):
    """Same text, different question, is a different answer and must be asked."""
    book = tmp_path / "run.jsonl"
    server = Answering()
    for question in (QUESTION, "Is this spam?"):
        client = a_real_client(server.url, "threads", pack=1, workers=1)
        try:
            client.classify(["one text"], question, checkpoint=book)
        finally:
            client.close()
    asked = server.seen
    server.close()
    assert asked == 2, "a checkpoint answered a question it had never been asked"


def test_a_stream_keeps_one_checkpoint_across_its_chunks(tmp_path):
    book = tmp_path / "run.jsonl"
    server = Answering()
    client = a_real_client(server.url, "threads", pack=2, workers=2)
    try:
        answers = list(client.stream(iter(f"item {n}" for n in range(10)), QUESTION,
                                     chunk=4, checkpoint=book))
        assert len(client._ledgers) == 1, "the index was reread at every chunk boundary"
    finally:
        client.close()
    server.close()
    assert [a.item for a in answers] == [f"item {n}" for n in range(10)]
    assert len(records_in(book)) == 10


def test_a_half_written_last_line_is_survivable(tmp_path):
    """A hard kill can tear the line it was writing. The rest still counts."""
    book = tmp_path / "run.jsonl"
    server = Answering()
    client = a_real_client(server.url, "threads", pack=1, workers=1)
    try:
        client.classify([f"item {n}" for n in range(4)], QUESTION, checkpoint=book)
    finally:
        client.close()

    text = book.read_text(encoding="utf-8")
    book.write_text(text + '{"k":"deadbe', encoding="utf-8")   # torn mid-record

    second = a_real_client(server.url, "threads", pack=1, workers=1)
    try:
        answers = second.classify([f"item {n}" for n in range(4)], QUESTION, checkpoint=book)
    finally:
        second.close()
    asked = server.seen
    server.close()
    assert asked == 4, "a torn line cost the whole file"
    assert len(answers) == 4
    assert second.usage.resumed == 4


def test_pointing_a_checkpoint_at_the_wrong_file_is_refused(tmp_path):
    """Appending answers to somebody's data file is worse than failing."""
    from jev_ultralightspeed._ledger import NotACheckpoint

    other = tmp_path / "important.jsonl"
    other.write_text('{"id": 1, "text": "not ours"}\n', encoding="utf-8")
    client = Client(key="test-key", transport="threads")
    try:
        with pytest.raises(NotACheckpoint):
            client.classify(["one"], QUESTION, checkpoint=other)
    finally:
        client.close()


def test_the_digest_reads_a_record_without_parsing_it():
    """The fast path through a million lines, and its fallback."""
    from jev_ultralightspeed import _ledger

    line = b'{"k":"' + b"ab" * 16 + b'","p":0.9,"l":"yes"}\n'
    assert _ledger._key_in(line) == bytes.fromhex("ab" * 16)
    assert _ledger._key_in(b'{"p":0.9,"k":"' + b"cd" * 16 + b'"}\n') == bytes.fromhex("cd" * 16)
    assert _ledger._key_in(b'{"k":"deadbe') is None
    assert _ledger._key_in(b"\n") is None


# -- skipping the rows that cannot be done ----------------------------------

@both_transports
def test_one_bad_row_cannot_wedge_a_checkpointed_job(transport, tmp_path):
    """
    The two features fight each other without this. One unreadable answer ends
    the run; the checkpoint means the rerun resumes, reaches the same row, and
    ends in the same place. Forever, and without saying which row.
    """
    book = tmp_path / "run.jsonl"
    server = Answering(poison="row 37")
    items = [f"row {n}" for n in range(100)]

    client = a_real_client(server.url, transport, pack=8, workers=4)
    try:
        with pytest.raises(JevError):                    # the old behaviour, still the default
            client.classify(items, QUESTION, checkpoint=book)
    finally:
        client.close()

    first = a_real_client(server.url, transport, pack=8, workers=4)
    try:
        answers = first.classify(items, QUESTION, checkpoint=book, on_error="skip")
    finally:
        first.close()

    assert len(answers) == 100
    assert [a.item for a in answers] == items
    bad = [a for a in answers if not a.ok]
    assert [a.item for a in bad] == ["row 37"], "the wrong rows were given up on"
    assert bad[0].error and "unreadable" in bad[0].error
    assert first.usage.skipped == 1
    assert [a.item for a in first.failures] == ["row 37"]
    assert len(records_in(book)) == 99, "the row with no answer was banked as if it had one"

    before = server.seen
    second = a_real_client(server.url, transport, pack=8, workers=4)
    try:
        again = second.classify(items, QUESTION, checkpoint=book, on_error="skip")
    finally:
        second.close()
    server.close()

    assert len(again) == 100
    assert second.usage.resumed == 99
    assert server.seen - before == 1, "the second run re-asked more than the row that failed"


@both_transports
def test_skipping_does_not_swallow_a_wrong_key(transport):
    """
    The budget is what stops `skip` turning a broken run into a million empty
    answers. Five requests' worth of items, then it gives up and says so.
    """
    server = Counting()
    client = a_real_client(server.url, transport, pack=8, workers=4)
    try:
        with pytest.raises(JevError):
            client.classify([f"row {n}" for n in range(1000)], QUESTION, on_error="skip")
    finally:
        client.close()
        server.close()
    assert client.usage.skipped <= 40 + 8, f"{client.usage.skipped} items skipped before giving up"
    assert server.seen < 100, f"{server.seen} requests sent for a run that was never going to work"


@both_transports
def test_a_request_that_fails_for_good_marks_every_item_in_it(transport):
    """A whole request is gone, so the items are gone together, in place."""
    server = Answering(fail_after=2)
    client = a_real_client(server.url, transport, pack=4, workers=1)
    items = [f"row {n}" for n in range(16)]
    try:
        answers = client.classify(items, QUESTION, on_error="skip")
    finally:
        client.close()
        server.close()

    assert [a.item for a in answers] == items
    assert sum(1 for a in answers if not a.ok) == 8, "two failed requests, four items each"
    assert client.usage.skipped == 8
    assert all(a.label == "" and a.p == 0.0 for a in answers if not a.ok)


def test_on_error_takes_one_of_two_words():
    client = Fake()
    with pytest.raises(JevError, match="on_error"):
        client.classify(["one"], QUESTION, on_error="ignore")


def test_a_checkpoint_is_per_pack_depth(tmp_path):
    """
    bench_packing.py measures a 3.3 point spread by position in a request, so an
    answer from pack=32 is not the answer pack=1 would have given. Serving one
    for the other is the quiet kind of wrong.
    """
    book = tmp_path / "run.jsonl"
    server = Answering()
    texts = [f"row {n}" for n in range(8)]
    for pack in (8, 1):
        client = a_real_client(server.url, "threads", pack=pack, workers=1)
        try:
            client.classify(texts, QUESTION, checkpoint=book)
        finally:
            client.close()
    asked = server.seen
    server.close()
    assert asked == 1 + 8, "a checkpoint from pack=8 was served to a pack=1 run"


def test_usage_does_not_call_resumed_items_speed(tmp_path):
    """
    `usage` is the evidence behind every claim in the README, so the one number
    in it that was not true was worth fixing: a resumed run reported millions of
    items a second.
    """
    book = tmp_path / "run.jsonl"
    server = Answering()
    items = [f"row {n}" for n in range(20)]
    for _ in range(2):
        client = a_real_client(server.url, "threads", pack=4, workers=2)
        try:
            client.classify(items, QUESTION, checkpoint=book)
        finally:
            client.close()
    server.close()

    assert client.usage.resumed == 20
    assert client.usage.answered == 0
    assert client.usage.items_per_second == 0.0
    assert client.usage.tokens_per_item == 0.0
    assert "20 resumed" in str(client.usage)


# -- the rolling window -----------------------------------------------------

@both_transports
def test_a_straggler_does_not_hold_back_what_is_already_done(transport):
    """
    Consuming results in input order means one slow request holds back the
    banking, the progress and the error of everything behind it that has already
    finished. Measured locally before this: a request back in 13ms, reported at
    352ms.
    """
    server = Answering(delay=0.01, slow_for="item 0", slow_by=0.6)
    client = a_real_client(server.url, transport, pack=1, workers=4)
    seen = []
    started = time.monotonic()
    try:
        answers = client.classify([f"item {n}" for n in range(16)], QUESTION,
                                  on_progress=lambda done, total: seen.append(
                                      (done, time.monotonic() - started)))
    finally:
        client.close()
        server.close()

    assert len(answers) == 16
    assert [a.item for a in answers] == [f"item {n}" for n in range(16)]
    first = seen[0][1]
    assert first < 0.4, f"the first report waited {first:.2f}s behind the straggler"
    assert seen[-1][0] == 16


def test_a_fatal_error_stops_the_queue_rather_than_draining_it():
    """
    A failure in a later group used to go unnoticed until the iterator reached
    it. Behind one slow first group, the pool churned through the rest while
    nobody was looking: 64 of 64 started, in the probe.
    """
    server = Answering(slow_for="item 0", slow_by=0.5, fail_unless="item 0")
    client = a_real_client(server.url, "threads", pack=1, workers=8)
    try:
        with pytest.raises(JevError):
            client.classify([f"item {n}" for n in range(64)], QUESTION)
    finally:
        client.close()
        server.close()
    assert server.seen < 20, f"{server.seen} of 64 requests went out after the run was doomed"


# -- a copy of a failure is still a failure ---------------------------------

def test_a_duplicate_of_a_skipped_item_is_also_skipped():
    """
    The copy came back saying `ok` True with no answer in it and no error, which
    is worse than either a failure or an exception.
    """
    server = Answering(poison="same")
    client = a_real_client(server.url, "threads", pack=4, workers=1)
    try:
        answers = client.classify(["same", "same"], QUESTION, on_error="skip")
    finally:
        client.close()
        server.close()

    assert [a.ok for a in answers] == [False, False]
    assert all(a.error and "unreadable" in a.error for a in answers)
    assert client.usage.skipped == 2, "a copy of nothing was counted as work done"
    assert client.usage.cached == 0


# -- warming that opens something -------------------------------------------

def test_threaded_warming_actually_opens_the_connections():
    """Constructing an HTTPConnection opens nothing. This measured zero before."""
    import http.client

    opened = []
    real = http.client.HTTPConnection.connect

    def counted(self):
        opened.append(1)
        return real(self)

    http.client.HTTPConnection.connect = counted
    server = Answering()
    client = a_real_client(server.url, "threads", pack=1, workers=3)
    try:
        client.warm()
    finally:
        http.client.HTTPConnection.connect = real
        client.close()
        server.close()
    assert len(opened) == 3, f"{len(opened)} of 3 connections were actually opened"


# -- packs that fit the request as well as the count ------------------------

def test_a_pack_is_split_when_the_items_will_not_fit_one_request():
    """
    `pack` counts items and the API refuses on tokens, so several individually
    legal items can make one illegal request.
    """
    from jev_ultralightspeed import CHARS_PER_TOKEN, MAX_STATE_TOKENS

    client = Fake(pack=64)
    big = "x" * 19_000                                  # legal on its own, 19k characters
    room = int(MAX_STATE_TOKENS * CHARS_PER_TOKEN)
    answers = client.classify([f"{big}{n}" for n in range(64)], QUESTION)
    assert len(answers) == 64
    packs = [len(body["state"]) for body in client.sent]
    assert len(packs) > 1, "64 items of 19,000 characters went out as one request"
    assert all(count * 19_000 < room for count in packs), packs
    assert max(packs) < 64


def test_small_items_still_pack_to_the_limit():
    client = Fake(pack=16)
    client.classify([f"item {n}" for n in range(32)], QUESTION)
    assert [len(body["state"]) for body in client.sent] == [16, 16]


def test_every_place_that_states_a_version_agrees():
    """
    The README badge sat at v0.3.1 through three releases, because the bump
    touched pyproject and __init__ and nothing checked the fourth place.
    """
    import re

    import jev_ultralightspeed

    root = Path(__file__).resolve().parents[1]
    packaged = re.search(r'^version = "([^"]+)"',
                         (root / "pyproject.toml").read_text(encoding="utf-8"),
                         re.M).group(1)
    badge = re.search(r"^\*\*v([0-9][^*]*)\*\*",
                      (root / "README.md").read_text(encoding="utf-8"), re.M).group(1)
    latest = re.search(r"^## v([0-9][^ ]*)",
                       (root / "CHANGELOG.md").read_text(encoding="utf-8"), re.M).group(1)
    assert jev_ultralightspeed.__version__ == packaged == badge == latest, {
        "__version__": jev_ultralightspeed.__version__, "pyproject": packaged,
        "README badge": badge, "CHANGELOG": latest,
    }


def test_the_readme_states_the_number_of_tests_there_are(request):
    """Hand-counted five times, wrong twice. Counted here instead."""
    import re

    if request.config.option.keyword or request.config.option.markexpr:
        pytest.skip("only meaningful when the whole suite ran")
    root = Path(__file__).resolve().parents[1]
    stated = int(re.search(r"# (\d+) tests", (root / "README.md").read_text(encoding="utf-8"))
                 .group(1))
    assert stated == len(request.session.items)


# -- the fast path, over real HTTP/2 ----------------------------------------

_TLS_EXCUSE = None          # worked out once per session, against a throwaway server


def an_h2_server():
    """
    The h2 fixture, or a skip saying why it cannot be used here.

    The interception check opens a connection of its own, without ALPN, and the
    thread that would record it can be scheduled after a reset, so it is done
    against a server that is then thrown away. Once per session.
    """
    global _TLS_EXCUSE
    from jev_ultralightspeed import _http2

    if not _http2.available():
        pytest.skip("httpx not installed")
    try:
        from h2_server import H2Server, client_context, intercepted
    except ImportError as error:                    # cryptography or h2 missing
        pytest.skip(f"the h2 fixture needs {error.name}; it comes with .[dev]")
    if _TLS_EXCUSE is None:
        throwaway = H2Server()
        try:
            _TLS_EXCUSE = intercepted(throwaway)
        finally:
            throwaway.close()
    if _TLS_EXCUSE:
        pytest.skip(_TLS_EXCUSE)
    server = H2Server()
    return server, client_context(server.ca_path)


def test_the_fast_path_really_negotiates_http2_and_multiplexes():
    """
    The claim the whole module exists for, and until now no test could see it.
    Every other server here speaks HTTP/1.1, and httpx falls back to HTTP/1.1
    whenever ALPN does not offer h2, silently, so "one connection, every request
    in flight on it" was taken on faith. This server offers h2 and nothing else,
    and counts the connections as well as the requests.
    """
    server, trust = an_h2_server()
    client = Client(key="k", url=server.url, pack=4, workers=4,
                    transport="http2", verify=trust)
    try:
        answers = client.classify([f"item {n}" for n in range(64)], QUESTION)
    finally:
        client.close()
        server.close()

    assert len(answers) == 64
    assert all(answer.ok for answer in answers)
    assert [a.item for a in answers] == [f"item {n}" for n in range(64)]
    assert client.http_version == "HTTP/2", f"fell back to {client.http_version}"
    assert server.protocols == {"h2"}
    assert server.seen == 16
    assert server.connections == 1, f"{server.connections} connections for 16 requests"
    assert server.streams > 1, "the requests went one at a time down one connection"


def test_the_threaded_path_says_which_protocol_it_used():
    """HTTP/1.1, and it should say so rather than leave the caller guessing."""
    server, trust = an_h2_server()
    client = Client(key="k", url=server.url, pack=4, workers=2,
                    transport="threads", verify=trust)
    try:
        with pytest.raises(JevError):        # an h2-only server refuses HTTP/1.1
            client.classify(["one"], QUESTION)
    finally:
        client.close()
        server.close()
    assert client.http_version == "HTTP/1.1"


def test_bringing_your_own_trust_still_offers_http2():
    """
    httpx sets ALPN on contexts it builds and not on one it is handed, so a
    caller with a private CA for their own gateway would have lost the fast path
    without being told. There is no public getter for a context's ALPN list, so
    the call is recorded instead.
    """
    import ssl as ssl_module

    from jev_ultralightspeed import _http2

    if not _http2.available():
        pytest.skip("httpx not installed")
    stdlib = next(cls for cls in ssl_module.SSLContext.__mro__ if cls.__module__ == "ssl")
    offered = []

    class Recording(stdlib):
        def set_alpn_protocols(self, protocols):
            offered.append(list(protocols))
            return super().set_alpn_protocols(protocols)

    client = Client(key="k", url="https://example.invalid/v1", transport="http2",
                    verify=Recording(ssl_module.PROTOCOL_TLS_CLIENT))
    try:
        client._pipe_for()                       # builds nothing on the network
    finally:
        client.close()
    assert offered, "nobody set ALPN on the caller's own context"
    assert "h2" in offered[0], offered


# -- carrying the question once ---------------------------------------------

def test_guidance_once_puts_the_question_in_the_state_and_points_at_it():
    from jev_ultralightspeed import GUIDANCE, _packed_body

    instructions = "Does this need a human today?"
    criteria = {"true": "broken now", "false": "can wait"}
    body = _packed_body("m", asks_for(["a", "b", "c"], instructions, criteria=criteria),
                        "once")

    assert body["state"][GUIDANCE].startswith(instructions)
    assert "true: broken now" in body["state"][GUIDANCE]
    assert set(body["state"]) == {"item_1", "item_2", "item_3", GUIDANCE}
    for name, question in body["questions"].items():
        assert instructions not in question["instructions"], "the question is still repeated"
        assert GUIDANCE in question["instructions"]
        assert name in question["instructions"]
        assert "criteria" not in question


def test_the_option_names_stay_in_every_question():
    """
    For a pick-one question the options are the answer space, not wording, so
    they are not the part that moves.
    """
    from jev_ultralightspeed import GUIDANCE, _packed_body

    options = {"yes_please": "a", "no_thanks": "b"}
    body = _packed_body("m", asks_for(["a", "b"], "Which?", options=options), "once")
    assert body["state"][GUIDANCE] == "Which?"
    for question in body["questions"].values():
        assert question["criteria"] == options
        assert question["type"] == "choice"


def test_an_unpacked_request_is_unchanged_by_guidance():
    """One item has nothing to share the question with."""
    client = Fake(pack=1, guidance="once")
    client.classify(["only"], QUESTION)
    from jev_ultralightspeed import GUIDANCE

    assert GUIDANCE not in client.sent[0]["state"]
    assert QUESTION in client.sent[0]["questions"]["item_1"]["instructions"]


def test_the_shape_of_the_question_is_part_of_the_key():
    """
    A different prompt is a different answer, so a cache or a checkpoint must not
    serve one for the other.
    """
    repeated = Fake(pack=4)
    once = Fake(pack=4, guidance="once")
    assert (repeated._shape(QUESTION, None, None) != once._shape(QUESTION, None, None))
    shape = repeated._shape(QUESTION, None, None)
    assert repeated._key("t", shape) != repeated._key("u", shape)
    assert Fake(pack=8)._shape(QUESTION, None, None) != shape, "pack must be in there too"


def test_guidance_takes_one_of_two_words():
    with pytest.raises(JevError, match="guidance"):
        Client(key="k", guidance="hoisted")


def test_a_shared_question_still_reads_every_answer():
    server = Answering()
    client = a_real_client(server.url, "threads", pack=8, workers=2, guidance="once")
    try:
        answers = client.classify([f"item {n}" for n in range(16)], QUESTION)
    finally:
        client.close()
        server.close()
    assert [a.item for a in answers] == [f"item {n}" for n in range(16)]
    assert all(a.ok for a in answers)
    assert server.seen == 2


# -- the rolling stream -----------------------------------------------------

@both_transports
def test_a_stream_hands_back_answers_before_the_chunk_is_done(transport):
    """
    It used to classify a whole chunk and only then yield any of it, so the first
    answer of a 5,000-item chunk waited on all 157 of its requests. Here the last
    request is the slow one, and everything before it should already be out.
    """
    server = Answering(delay=0.02, slow_for="item 63", slow_by=0.6)
    client = a_real_client(server.url, transport, pack=4, workers=4)
    arrived = []
    started = time.monotonic()
    try:
        for answer in client.stream(iter(f"item {n}" for n in range(64)), QUESTION, chunk=64):
            arrived.append((answer.item, time.monotonic() - started))
    finally:
        client.close()
        server.close()

    assert [item for item, _ in arrived] == [f"item {n}" for n in range(64)]
    assert arrived[0][1] < 0.35, f"the first answer waited {arrived[0][1]:.2f}s"
    assert arrived[-1][1] > 0.4, "the straggler was not actually slow"
    assert arrived[0][1] < arrived[-1][1] / 2, arrived[0][1]


@both_transports
def test_a_repeat_waits_for_its_original_and_no_longer(transport):
    """
    Deduplication and order pull against each other in a stream: a repeat cannot
    be answered before the item it copies. It should wait for that one request,
    not for the rest of the chunk.
    """
    server = Answering(delay=0.02)
    client = a_real_client(server.url, transport, pack=2, workers=2)
    items = ["first", "second", "first", "third", "second"]
    try:
        answers = list(client.stream(iter(items), QUESTION, chunk=8))
    finally:
        client.close()
        server.close()

    assert [a.item for a in answers] == items
    assert all(a.ok for a in answers)
    assert client.usage.cached == 2, "the repeats were asked again"
    assert server.seen == 2, f"{server.seen} requests for three distinct items at pack=2"


def test_the_stream_reports_progress_too():
    server = Answering(delay=0.01)
    client = a_real_client(server.url, "threads", pack=4, workers=2)
    seen = []
    try:
        list(client.stream(iter(f"item {n}" for n in range(16)), QUESTION, chunk=8,
                           on_progress=lambda done, total: seen.append((done, total))))
    finally:
        client.close()
        server.close()
    assert seen, "a stream never said how far along it was"
    assert seen[-1][0] == seen[-1][1]


# -- which verdicts to trust ------------------------------------------------

def make_answers(probabilities, *, bad=()):
    from jev_ultralightspeed import Answer

    answers = []
    for index, p in enumerate(probabilities):
        if index in bad:
            answers.append(Answer(item=f"item {index}", p=0.0, label="", kind="error",
                                  error="unreadable"))
        else:
            answers.append(Answer(item=f"item {index}", p=p,
                                  label="yes" if p >= 0.5 else "no"))
    return answers


def test_triage_keeps_the_share_you_asked_for():
    from jev_ultralightspeed import triage

    answers = make_answers([0.99, 0.55, 0.97, 0.60, 0.95, 0.51, 0.93, 0.80, 0.91, 0.70])
    trusted, review = triage(answers, keep=0.8)
    assert len(trusted) == 8
    assert len(review) == 2
    assert {a.item for a in review} == {"item 1", "item 5"}, [a.item for a in review]
    assert [a.item for a in trusted] == [a.item for a in answers if a not in review]


def test_triage_takes_a_threshold_instead():
    from jev_ultralightspeed import triage

    answers = make_answers([0.99, 0.55, 0.91, 0.93])
    trusted, review = triage(answers, at_least=0.92)
    assert [a.item for a in trusted] == ["item 0", "item 3"]
    assert [a.item for a in review] == ["item 1", "item 2"]


def test_a_skipped_answer_is_always_one_to_look_at():
    """It has no probability to be confident about."""
    from jev_ultralightspeed import triage

    answers = make_answers([0.99, 0.99, 0.99, 0.99], bad=(2,))
    trusted, review = triage(answers, keep=1.0)
    assert [a.item for a in review] == ["item 2"]
    assert len(trusted) == 3


def test_triage_wants_exactly_one_way_of_saying_it():
    from jev_ultralightspeed import triage

    answers = make_answers([0.9])
    with pytest.raises(JevError):
        triage(answers, keep=0.5, at_least=0.5)
    with pytest.raises(JevError):
        triage(answers)
    with pytest.raises(JevError):
        triage(answers, keep=1.5)


def test_triage_keeps_the_order_it_was_given():
    from jev_ultralightspeed import triage

    answers = make_answers([0.2, 0.99, 0.3, 0.98, 0.4])
    trusted, review = triage(answers, keep=0.4)
    assert [a.item for a in trusted] == ["item 1", "item 3"]
    assert [a.item for a in review] == ["item 0", "item 2", "item 4"]


def test_keeping_nothing_trusts_nothing():
    from jev_ultralightspeed import triage

    answers = make_answers([0.99, 1.0])
    trusted, review = triage(answers, keep=0.0)
    assert trusted == []
    assert len(review) == 2


def test_the_question_cannot_change_underneath_a_run():
    """
    A stream is suspended between answers and the caller still holds the
    dictionaries they passed. Changing one used to change the questions still to
    be sent, halfway through a job.

    One worker and one item per request, so there are more requests to come than
    the window holds: with everything already queued, nothing is left to corrupt.
    """
    criteria = {"true": "broken now", "false": "can wait"}
    client = Fake(pack=1, workers=1)
    client.classify([f"item {n}" for n in range(10)], QUESTION, criteria=criteria,
                    on_progress=lambda done, total: criteria.update({"true": "CHANGED"}))
    assert criteria["true"] == "CHANGED", "the callback did not actually run"
    assert len(client.sent) == 10
    for body in client.sent:
        for question in body["questions"].values():
            assert question["criteria"]["true"] == "broken now", question["criteria"]


def test_a_mutated_question_does_not_poison_the_cache_key():
    """
    The identity is taken once, from the copy, so the items early in a call and
    the ones after the callback are remembered under the same key.
    """
    original = {"true": "broken now", "false": "can wait"}
    live = dict(original)
    items = [f"item {n}" for n in range(10)]
    client = Fake(pack=1, workers=1)
    client.classify(items, QUESTION, criteria=live,
                    on_progress=lambda done, total: live.update({"true": "CHANGED"}))
    already = client.usage.cached
    client.classify(items, QUESTION, criteria=dict(original))
    fresh = client.usage.cached - already
    assert fresh == len(items), f"only {fresh} of {len(items)} were filed under the real question"


# -- pacing -----------------------------------------------------------------

def test_an_unpaced_limiter_hands_out_the_whole_window_at_once():
    """The published ceiling is a minute's worth, and says nothing about a second."""
    limiter = _Limiter(600)
    assert all(limiter.try_take() == 0.0 for _ in range(600))
    assert limiter.try_take() > 0.0


def test_a_paced_limiter_puts_a_floor_under_the_gap():
    limiter = _Limiter(600, paced=True)          # a tenth of a second apart
    assert limiter.try_take() == 0.0
    wait = limiter.try_take()
    # A hair over the interval is fine and does happen: where the clock is coarse
    # enough that both reads land on the same instant, the answer is
    # (now + 0.1) - now, which in floating point is 0.10000000000002274 for a
    # `now` the size of an uptime. Windows CI found that; Linux never did.
    assert 0.0 < wait <= 0.1 + 1e-6, wait
    # Waited out with the blocking form rather than by sleeping for `wait` and
    # asking again. On Windows that sleep returns about 6ms early against
    # time.monotonic(), and the second ask says no. take() loops until it is
    # really allowed, which is the thing a caller does anyway.
    started = time.monotonic()
    limiter.take()
    assert time.monotonic() - started > 0.0, "it did not wait at all"



def test_pacing_does_not_bank_credit_while_nothing_is_running():
    """
    Waking after a quiet spell must not release everything the pace would have
    allowed meanwhile, which is the bug that makes a pacer worse than useless.
    """
    limiter = _Limiter(600, paced=True)
    assert limiter.try_take() == 0.0
    time.sleep(0.25)                             # two and a half intervals of nothing
    assert limiter.try_take() == 0.0             # one is owed
    assert limiter.try_take() > 0.0, "the pause was cashed in as a burst"


def test_a_paced_limiter_still_holds_the_minute():
    limiter = _Limiter(2, paced=True)            # thirty seconds apart, two a minute
    assert limiter.try_take() == 0.0
    assert 0.0 < limiter.try_take() <= 30.0


def test_the_window_lets_old_stamps_go():
    limiter = _Limiter(3)
    for _ in range(3):
        limiter.try_take()
    assert limiter.try_take() > 0.0
    limiter._recent[0] -= 61.0                   # as if that one were a minute ago
    assert limiter.try_take() == 0.0


@both_transports
def test_pacing_is_off_unless_asked_for(transport):
    server = Answering()
    client = a_real_client(server.url, transport, pack=4, workers=2)
    try:
        assert client._limiter._interval == 0.0
        client.classify([f"item {n}" for n in range(8)], QUESTION)
    finally:
        client.close()
        server.close()
    assert server.seen == 2


# -- certainty is not the probability of yes ---------------------------------

def test_certainty_reads_a_confident_no_as_confident():
    from jev_ultralightspeed import Answer

    assert Answer(item="x", p=0.01, label="no").certainty == 0.99
    assert Answer(item="x", p=0.99, label="yes").certainty == 0.99
    assert Answer(item="x", p=0.50, label="yes").certainty == 0.50
    assert Answer(item="x", p=0.60, label="yes").certainty == 0.60


def test_a_pick_one_answer_is_already_its_own_certainty():
    from jev_ultralightspeed import Answer

    answer = Answer(item="x", p=0.93, label="refusal", kind="choice",
                    distribution={"refusal": 0.93, "compliance": 0.07})
    assert answer.certainty == 0.93


def test_a_skipped_answer_is_certain_of_nothing():
    """max(p, 1-p) on a skipped answer's p of zero would read as total confidence."""
    from jev_ultralightspeed import Answer

    assert Answer(item="x", p=0.0, label="", kind="error", error="unreadable").certainty == 0.0


def test_triage_prefers_a_confident_no_to_an_unsure_yes():
    """
    It used to rank yes/no answers by the probability of yes, so the answers it
    was surest about were the first ones thrown out.
    """
    from jev_ultralightspeed import triage

    answers = make_answers([0.60, 0.01])
    trusted, review = triage(answers, keep=0.5)
    assert [a.item for a in trusted] == ["item 1"], "the confident no was not the one kept"
    assert [a.item for a in review] == ["item 0"]


def test_triage_with_a_threshold_also_reads_certainty():
    from jev_ultralightspeed import triage

    trusted, review = triage(make_answers([0.05, 0.5, 0.95]), at_least=0.9)
    assert [a.item for a in trusted] == ["item 0", "item 2"]
    assert [a.item for a in review] == ["item 1"]


def test_keep_hands_back_the_share_asked_for_even_when_scores_tie():
    """Ten answers at the same score and two wanted used to hand back all ten."""
    from jev_ultralightspeed import triage

    answers = make_answers([0.9] * 10)
    trusted, review = triage(answers, keep=0.2)
    assert len(trusted) == 2, f"{len(trusted)} of 10 came back as trusted"
    assert len(review) == 8
    assert [a.item for a in trusted] == ["item 0", "item 1"], "ties are not broken by arrival"


def test_a_long_retry_after_is_honoured_not_shortened():
    """
    Clamping "wait two minutes" to sixty seconds means coming back early against
    the one instruction the server gave.
    """
    from jev_ultralightspeed import _http2

    assert 120.0 <= _http2._backoff(0, "120") <= 121.0
    assert 240.0 <= _http2._backoff(5, "240") <= 241.0
    assert _http2._backoff(9, None) <= 60.0, "our own doubling is still bounded"


def test_a_ridiculous_retry_after_is_refused_rather_than_slept_on():
    from jev_ultralightspeed import JevError, _http2

    with pytest.raises(JevError, match="longer than"):
        _http2._backoff(0, "3600")


@both_transports
def test_a_retry_records_what_pushed_it_back(transport):
    """
    A retry count that cannot say why generates mysteries. The headline benchmark
    took 245 of them and kept no record of whether they were 429s, 529s or a
    dropped connection, so it can never be settled.
    """
    server = Answering(push_back=3, status=429)
    client = a_real_client(server.url, transport, pack=4, workers=1)
    try:
        answers = client.classify([f"item {n}" for n in range(8)], QUESTION)
    finally:
        client.close()
        server.close()

    assert len(answers) == 8
    assert all(a.ok for a in answers)
    assert client.usage.retries == 3
    assert client.usage.pushback == {429: 3}, client.usage.pushback
    assert client.usage.waited > 0.0
    assert "429" in client.usage.why_retried
    assert "429" in str(client.usage)


@both_transports
def test_each_status_is_counted_under_itself(transport):
    """A 503 and a 429 are different problems and should not share a bucket."""
    server = Answering(push_back=2, status=503)
    client = a_real_client(server.url, transport, pack=4, workers=1)
    try:
        client.classify([f"item {n}" for n in range(8)], QUESTION)
    finally:
        client.close()
        server.close()
    assert client.usage.pushback == {503: 2}, client.usage.pushback


def test_several_clients_can_hold_one_ceiling_between_them():
    """
    Two clients taking turns with a limiter each would let the pair send twice what
    either is allowed, which is what made the headline benchmark's two arms hard to
    compare. A shared limiter is also the hook for a ceiling held across machines.
    """
    shared = _Limiter(6)
    one = Fake(limiter=shared)
    two = Fake(limiter=shared)
    assert one._limiter is two._limiter
    assert all(one._limiter.try_take() == 0.0 for _ in range(3))
    assert all(two._limiter.try_take() == 0.0 for _ in range(3))
    assert one._limiter.try_take() > 0.0, "the pair went over the shared ceiling"
    assert two._limiter.try_take() > 0.0


def test_a_client_without_one_gets_its_own():
    first, second = Fake(), Fake()
    assert first._limiter is not second._limiter


# -- a question of its own --------------------------------------------------

@both_transports
def test_rows_with_their_own_answer_space_still_share_a_request(transport):
    """
    The case this exists for. When every row picks from its own set of labels
    there is nothing to group by, so without it each row is a pack of one.
    """
    from jev_ultralightspeed import Ask

    server = Answering()
    client = a_real_client(server.url, transport, pack=8, workers=2)
    asks = [Ask(f"row {n}", options={f"label_{n}": "a", "other": "b"}) for n in range(16)]
    try:
        answers = client.judge(asks, instructions="Which applies?")
    finally:
        client.close()
        server.close()

    assert [a.item for a in answers] == [f"row {n}" for n in range(16)]
    assert server.seen == 2, f"{server.seen} requests for 16 rows at pack=8"
    sent = json.loads(server.bodies[0])
    assert len(sent["questions"]) == 8
    spaces = [tuple(sorted(q["criteria"])) for q in sent["questions"].values()]
    assert len(set(spaces)) == 8, "the rows were given the same answer space"


def test_an_ask_falls_back_to_what_the_call_was_given():
    from jev_ultralightspeed import Ask

    client = Fake(pack=4)
    client.judge([Ask("a"), Ask("b", instructions="something else")],
                 instructions="the usual", criteria={"true": "t", "false": "f"})
    questions = list(client.sent[0]["questions"].values())
    assert "the usual" in questions[0]["instructions"]
    assert "something else" in questions[1]["instructions"]
    assert questions[0]["criteria"] == {"true": "t", "false": "f"}
    assert questions[1]["criteria"] == {"true": "t", "false": "f"}, "criteria should carry over"


def test_the_same_row_asked_two_things_is_two_answers():
    """Deduplication keys on the question as well as the text."""
    from jev_ultralightspeed import Ask

    client = Fake(pack=8)
    answers = client.judge([Ask("same text", instructions="first question"),
                            Ask("same text", instructions="second question"),
                            Ask("same text", instructions="first question")],
                           instructions="unused")
    assert len(answers) == 3
    asked = sum(len(body["state"]) for body in client.sent)
    assert asked == 2, f"{asked} rows sent; the repeat of the first question should collapse"
    assert client.usage.cached == 1


def test_judge_says_so_when_handed_plain_text():
    from jev_ultralightspeed import Ask

    client = Fake()
    with pytest.raises(JevError, match="Ask objects"):
        client.judge(["just a string"], instructions="q")
    with pytest.raises(JevError, match="no instructions"):
        client.judge([Ask("a")])


def test_a_mixed_pack_keeps_its_questions_where_they_are():
    """There is nothing to hoist when the rows are not asking the same thing."""
    from jev_ultralightspeed import GUIDANCE, Ask

    client = Fake(pack=4, guidance="once")
    client.judge([Ask("a", instructions="one"), Ask("b", instructions="two")])
    assert GUIDANCE not in client.sent[0]["state"]
    client.sent.clear()
    client.judge([Ask("c"), Ask("d")], instructions="the same for both")
    assert GUIDANCE in client.sent[0]["state"], "a uniform pack should still hoist"


def test_the_question_is_serialised_once_per_distinct_question_not_per_row():
    """The v0.9.1 saving has to survive rows carrying their own questions."""
    from jev_ultralightspeed import Ask

    shared = {"true": "t", "false": "f"}
    client = Fake(pack=64)
    calls = []
    original = client._shape
    client._shape = lambda *a: (calls.append(1), original(*a))[1]
    client.judge([Ask(f"row {n}", criteria=shared) for n in range(64)],
                 instructions="the same question")
    assert len(calls) == 1, f"the question was serialised {len(calls)} times for 64 rows"


# -- scoring along a list ---------------------------------------------------

LEVELS = ["Calm", "Frustrated", "Very angry"]


class Scoring(Client):
    """A client whose score questions come back the way the API documents them."""

    def __init__(self, where=1.05, **kwargs):
        kwargs.setdefault("transport", "threads")
        super().__init__(key="test-key", **kwargs)
        self.sent = []
        self.where = where

    def ask(self, body):
        self.sent.append(body)
        answers = {}
        for name in body["state"]:
            if not name.startswith("item_"):
                continue
            answers[name] = {"type": "score", "score": self.where,
                             "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
                             "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
                             "confidence": 0.92}
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 1}}


def test_a_score_question_sends_an_ordered_list():
    client = Scoring(pack=4)
    client.classify(["a customer wrote in"], "How frustrated are they?", levels=LEVELS)
    question = client.sent[0]["questions"]["item_1"]
    assert question["type"] == "score"
    assert question["criteria"] == LEVELS, "the levels must stay in order and stay a list"


def test_a_score_answer_keeps_the_number_and_names_the_level():
    client = Scoring(pack=4)
    answer = client.classify(["x"], "How frustrated?", levels=LEVELS)[0]
    assert answer.kind == "score"
    assert answer.score == 1.05, "the point of a score is that it lands between levels"
    assert answer.label == "Frustrated", "the nearest level, by name"
    assert answer.p == 0.95
    assert answer.distribution == {"Calm": 0.0, "Frustrated": 0.95, "Very angry": 0.05}
    assert answer.confidence == 0.92
    assert answer.certainty == 0.95


def test_a_score_at_the_top_of_the_range_is_not_off_the_end():
    client = Scoring(where=2.4, pack=4)
    answer = client.classify(["x"], "How frustrated?", levels=LEVELS)[0]
    assert answer.label == "Very angry", answer.label
    assert answer.score == 2.4


def test_levels_stay_in_every_question_like_options_do():
    """They are the answer space, not wording, so there is nothing to hoist."""
    from jev_ultralightspeed import GUIDANCE

    client = Scoring(pack=4, guidance="once")
    client.classify(["a", "b"], "How frustrated?", levels=LEVELS)
    body = client.sent[0]
    assert body["state"][GUIDANCE] == "How frustrated?"
    for question in body["questions"].values():
        assert question["criteria"] == LEVELS


def test_a_question_is_one_shape_or_the_other():
    client = Scoring()
    with pytest.raises(JevError, match="not both"):
        client.classify(["x"], "q", options={"a": "A"}, levels=LEVELS)


def test_the_levels_are_part_of_the_key():
    """Scoring against a different ladder is a different answer."""
    one = Scoring(pack=4)
    assert (one._shape("q", None, None, ["low", "high"])
            != one._shape("q", None, None, ["low", "middle", "high"]))
    assert one._shape("q", None, None, None) != one._shape("q", None, None, ["low", "high"])


def test_one_request_can_mix_all_three_kinds_of_question():
    """The API takes them together, and judge() is how you say that."""
    from jev_ultralightspeed import Ask

    client = Scoring(pack=8)
    client.judge([Ask("a", instructions="Urgent?"),
                  Ask("b", instructions="Which team?", options={"billing": "b", "tech": "t"}),
                  Ask("c", instructions="How frustrated?", levels=LEVELS)])
    kinds = [q["type"] for q in client.sent[0]["questions"].values()]
    assert kinds == ["noul", "choice", "score"], kinds
    assert len(client.sent) == 1, "three kinds of question, one request"


def test_a_resumed_score_still_has_its_number(tmp_path):
    book = tmp_path / "run.jsonl"
    first = Scoring(pack=4)
    first.classify(["x", "y"], "How frustrated?", levels=LEVELS, checkpoint=book)
    first.close()
    second = Scoring(pack=4)
    answers = second.classify(["x", "y"], "How frustrated?", levels=LEVELS, checkpoint=book)
    second.close()
    assert second.usage.resumed == 2
    assert [a.score for a in answers] == [1.05, 1.05]
    assert [a.label for a in answers] == ["Frustrated", "Frustrated"]


def test_the_cache_is_keyed_on_the_pack_you_asked_for_not_the_one_you_got():
    """
    Deliberate, and worth pinning rather than leaving to be rediscovered. A call
    of 33 items at pack=32 sends a request of 32 and a request of 1, and that lone
    answer is filed under the same key as the other 32.

    Placement cannot be part of the key because it is not a function of the input:
    deduplication and cache hits change the grouping, so the same call made twice
    can put the same row in a different sized request. A key that depended on it
    would miss almost every time. `Answer.packed` is how a caller sees what
    actually happened.
    """
    client = Fake(pack=32)
    first = client.classify([f"row {n}" for n in range(33)], QUESTION)
    assert [len(body["state"]) for body in client.sent] == [32, 1]
    assert first[-1].packed == 1, "the last item rode alone"

    client.sent.clear()
    again = client.classify(["row 32"] + [f"other {n}" for n in range(31)], QUESTION)
    assert client.usage.cached == 1, "the lone answer was served into a full pack"
    assert again[0].packed == 1, "and it still says the depth it was answered at"
    assert len(client.sent[0]["state"]) == 31, "the other 31 went without it"


def test_the_token_estimate_can_be_told_about_denser_text():
    """3.5 characters a token is an English number. Code and CJK are far denser."""
    # Distinct, or deduplication collapses them to one and there is nothing to plan.
    items = ["\u6771\u4eac" * 2_000 + str(n) for n in range(20)]   # 4,000 characters each
    dense = Fake(pack=64, chars_per_token=1.0)
    roomy = Fake(pack=64, chars_per_token=3.5)
    dense.classify(items, QUESTION)
    roomy.classify(items, QUESTION)
    biggest_dense = max(len(body["state"]) for body in dense.sent)
    biggest_roomy = max(len(body["state"]) for body in roomy.sent)
    assert biggest_dense < biggest_roomy, (biggest_dense, biggest_roomy)
    assert dense.chars_per_token == 1.0


# -- the key does not go out in the clear -----------------------------------

def test_plain_http_to_another_machine_is_refused():
    """
    A bearer token over plain http is a key read by anything on the path, and the
    way that happens is a typo in a hostname rather than a decision.
    """
    for url in ("http://example.internal/v1", "http://10.0.0.5/v1", "http://a-typo.com/v1"):
        with pytest.raises(JevError, match="in the clear"):
            Client(key="sk-secret", url=url, transport="threads")


def test_the_refusal_does_not_repeat_the_key_back():
    try:
        Client(key="sk-a-real-looking-secret", url="http://a-typo.com/v1", transport="threads")
    except JevError as error:
        assert "sk-a-real-looking-secret" not in str(error)
    else:
        raise AssertionError("it was not refused")


def test_plain_http_to_this_machine_is_fine():
    """Loopback is a test double or a local gateway, which is what http is for."""
    for url in ("http://127.0.0.1:8080/v1", "http://localhost/v1", "http://[::1]/v1"):
        Client(key="k", url=url, transport="threads").close()


def test_a_plaintext_gateway_can_be_opted_into():
    client = Client(key="k", url="http://gateway.internal/v1", transport="threads",
                    allow_insecure_http=True)
    client.close()


def test_https_anywhere_is_fine():
    Client(key="k", url="https://anything.example/v1", transport="threads").close()
