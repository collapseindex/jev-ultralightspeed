"""
What the client must do without touching the network: pack, dedupe, cache,
keep the order, and read every answer shape. `ask` is replaced by a fake that
answers from the request it is given, so these run anywhere, for free.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev_ultralightspeed import (Answer, Client, JevError, _Limiter,          # noqa: E402
                                 _packed_body, _read, classify)

QUESTION = "Does this need a human today?"


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
    body = _packed_body("jev-latest", ["first", "second"], QUESTION, None, None)
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


def test_backoff_uses_the_server_hint_and_jitters_otherwise():
    from jev_ultralightspeed import _http2

    assert _http2._backoff(3, "7") == 7.0                     # the server said seven seconds
    assert 0.5 <= _http2._backoff(0, "next tuesday") <= 1.5   # unreadable hint, fall through
    waits = {_http2._backoff(4, None) for _ in range(20)}
    assert len(waits) > 1, "identical backoff means the workers collide again"
    assert all(8.0 <= wait <= 24.0 for wait in waits), sorted(waits)[:3]


def test_the_rate_limit_reaches_the_fast_path_too():
    from jev_ultralightspeed import _http2
    import inspect

    source = inspect.getsource(_http2.Pipe._one)
    assert "_limiter.take()" in source, "the http2 path must hold the same ceiling"
    assert source.index("_gate") < source.index("_limiter.take()")
    # And the waiting has to happen outside the semaphore.
    assert source.index("async with self._gate") < source.index("await asyncio.sleep(wait)")
    assert "return_exceptions=True" in inspect.getsource(_http2.Pipe._all)
