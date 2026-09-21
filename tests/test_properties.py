"""
Ten thousand items through a server that misbehaves, and the things that must
hold anyway.

The unit tests drive one behaviour each against a server that does what it is
told. These do the opposite: a lot of items, a randomised client, and a server
that refuses, stalls, drops connections, answers short and answers nonsense at
rates chosen by a seed. Then they check the promises that do not depend on any
of that:

    every item gets exactly one answer, in the order it was given
    a repeat of a text gets the same answer as its original
    a second run with the same checkpoint asks for nothing
    a checkpoint never holds an answer for an item that was skipped
    what usage says adds up to what came back

Packing, caching, checkpoints and failures each have their own tests. What has
not been exercised is all four at once at volume, which is where anything left is
likely to be. Seeds are printed on failure, and re-running one reproduces it.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev_ultralightspeed import Answer, Ask, Client, JevError  # noqa: E402

ITEMS = int(os.environ.get("JEV_PROPERTY_ITEMS", "10000"))
SEEDS = [1, 2, 3]


class Chaotic:
    """
    A server that answers correctly most of the time and misbehaves the rest,
    at rates a seed decides. Everything it does is something the real one can.
    """

    def __init__(self, seed: int, *, trouble: float = 0.15, spoiled: float = 0.004):
        self.seen = 0
        self.lock = threading.Lock()
        self.dice = random.Random(seed)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                asked = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                with server.lock:
                    server.seen += 1
                    roll = server.dice.random()
                    spoil = server.dice.random()
                names = [n for n in asked["state"] if n.startswith("item_")]
                # Trouble that a retry fixes can be common, and is: the client is
                # supposed to ride these out and the run is supposed to finish.
                if roll < trouble * 0.5:                      # pushed back, then fine
                    return self.reply(429, {"detail": "slow down"}, retry_after="0.01")
                if roll < trouble * 0.8:                      # briefly unavailable
                    return self.reply(503, {"detail": "back shortly"})
                if roll < trouble:                            # the connection just goes
                    self.close_connection = True
                    return
                # Trouble a retry cannot fix has to stay rare, because `skip`
                # tolerates one percent of the items and then abandons the run,
                # which is the behaviour a different test is for.
                answers = {}
                for index, name in enumerate(names):
                    if index == 0 and spoil < spoiled * 0.5:
                        continue                              # one item missing from the payload
                    if index == 0 and spoil < spoiled:
                        answers[name] = {"type": "noul", "shrug": "no idea"}   # unreadable
                    else:
                        answers[name] = {"type": "noul", "noul": 0.8}
                self.reply(200, {"model": "jev-1.13.0", "answers": answers,
                                 "usage": {"input_tokens": 10 * len(names),
                                           "output_tokens": len(names)}})

            def reply(self, status, payload, retry_after=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                if retry_after:
                    self.send_header("retry-after", retry_after)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def a_corpus(dice: random.Random, count: int) -> list[str]:
    """Items of mixed length, with repeats, because both are ordinary."""
    rows = []
    while len(rows) < count:
        if rows and dice.random() < 0.15:
            rows.append(dice.choice(rows))                    # a repeat of something earlier
        else:
            size = dice.choice([20, 80, 400, 2_000])
            rows.append(f"row {len(rows)} " + "x" * size)
    return rows


def a_shape(dice: random.Random, url: str) -> dict:
    """
    One client's worth of settings.

    Taken once and reused where a checkpoint has to be read back, because `pack`
    and `guidance` are part of the key on purpose: a rerun with a different pack
    is a different question and correctly finds nothing.

    pack=1 and a single worker are covered by the unit tests. Here they only buy
    minutes of jittered backoff, so these runs use shapes a volume job would use.
    """
    from jev_ultralightspeed import _http2

    transports = ["threads"] + (["http2"] if _http2.available() else [])
    return dict(key="test-key", url=url, transport=dice.choice(transports),
                pack=dice.choice([8, 32, 64]), workers=dice.choice([4, 8]),
                cache=dice.choice([True, False]), dedupe=dice.choice([True, False]),
                requests_per_minute=100_000)


def holds(items: list[str], answers: list[Answer], client: Client) -> None:
    """The promises that do not depend on how the server behaved."""
    assert len(answers) == len(items), "an item went missing or was answered twice"
    assert [a.item for a in answers] == items, "the order or the text changed"
    assert client.usage.items == len(items)
    assert client.usage.answered == client.usage.items - client.usage.cached - client.usage.resumed

    # A repeat of a text carries the same verdict as whatever answered it first.
    first: dict[str, Answer] = {}
    for answer in answers:
        seen = first.setdefault(answer.item, answer)
        assert (answer.label, answer.p, answer.ok) == (seen.label, seen.p, seen.ok), \
            f"two different answers for the same text: {answer.item[:30]!r}"

    skipped = [a for a in answers if not a.ok]
    assert all(a.error for a in skipped), "a skipped answer with no reason"
    assert client.usage.skipped == len(skipped), \
        f"usage says {client.usage.skipped} skipped, {len(skipped)} came back that way"


@pytest.mark.parametrize("seed", SEEDS)
def test_ten_thousand_items_through_a_server_having_a_bad_day(seed):
    dice = random.Random(seed)
    server = Chaotic(seed)
    items = a_corpus(dice, ITEMS)
    client = Client(**a_shape(dice, server.url))
    try:
        answers = client.classify(items, "Does this need a human?", on_error="skip")
    finally:
        client.close()
        server.close()
    holds(items, answers, client)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_checkpoint_survives_the_same_bad_day(seed, tmp_path):
    """
    The second run must ask for nothing it already has, come back the same, and
    never have banked an answer for an item it could not do.
    """
    dice = random.Random(seed + 100)
    server = Chaotic(seed + 100)
    items = a_corpus(dice, ITEMS // 4)
    book = tmp_path / "run.jsonl"
    question = "Does this need a human?"

    shape = a_shape(dice, server.url)
    client = Client(**shape)
    try:
        first = client.classify(items, question, checkpoint=book, on_error="skip")
    finally:
        client.close()
    holds(items, first, client)
    # Records, not keys: the ledger is append-only, so with dedupe off the same
    # text is written more than once and the last line wins. What must be exact
    # is the set of keys.
    written = [line for line in book.read_text(encoding="utf-8").splitlines()[1:] if line]
    keys = {json.loads(line)["k"] for line in written}
    skipped = sum(1 for a in first if not a.ok)

    before = server.seen
    second = Client(**shape)                       # the same shape, or the key will not match
    try:
        again = second.classify(items, question, checkpoint=book, on_error="skip")
    finally:
        second.close()
        server.close()
    holds(items, again, second)

    distinct = len({a.item for a in first if a.ok})
    assert len(keys) == distinct, f"{len(keys)} keys banked for {distinct} distinct answers"
    assert len(written) >= len(keys)
    assert second.usage.resumed > 0, "the checkpoint did nothing"
    asked = server.seen - before
    assert asked < len(items), f"the second run asked for {asked} of {len(items)} again"
    if skipped:
        # A skipped item is not banked, so it is the only thing a rerun retries.
        assert asked > 0


@pytest.mark.parametrize("seed", SEEDS)
def test_a_run_that_gives_up_still_keeps_what_it_had(seed, tmp_path):
    """With no skip budget the run raises, and what landed is still on disk."""
    dice = random.Random(seed + 200)
    server = Chaotic(seed + 200, trouble=0.0, spoiled=0.9)   # answers, but nonsense
    items = a_corpus(dice, 500)
    book = tmp_path / "run.jsonl"
    client = Client(**a_shape(dice, server.url))
    try:
        with pytest.raises(JevError):
            client.classify(items, "Does this need a human?", checkpoint=book)
    finally:
        client.close()
        server.close()

    assert all(isinstance(a, Answer) for a in client.last_partial)
    assert all(a.item in items for a in client.last_partial)
    if book.exists():
        banked = [line for line in book.read_text(encoding="utf-8").splitlines()[1:] if line]
        assert len(banked) <= len(items)


def test_mixed_question_shapes_at_volume():
    """judge() with all three kinds in the same pile, against the same server."""
    dice = random.Random(7)
    server = Chaotic(7, trouble=0.05)
    rows = a_corpus(dice, ITEMS // 5)
    asks = []
    for index, row in enumerate(rows):
        if index % 3 == 0:
            asks.append(Ask(row, instructions="Urgent?"))
        elif index % 3 == 1:
            asks.append(Ask(row, instructions="Which team?",
                            options={"billing": "b", "technical": "t"}))
        else:
            asks.append(Ask(row, instructions="How angry?", levels=["calm", "cross", "livid"]))
    client = Client(**a_shape(dice, server.url))
    try:
        answers = client.judge(asks, on_error="skip")
    finally:
        client.close()
        server.close()
    assert len(answers) == len(asks)
    assert [a.item for a in answers] == [ask.item for ask in asks]
    assert client.usage.items == len(asks)
