"""Offline client overhead. Run: python bench.py --offline --items 8000 --rounds 9.

No provider, network, or rate limiter is measured. Wall time includes planning,
unlike Usage.seconds. Compare the same command and Python on two revisions.
"""

import hashlib
import random
import statistics
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from jev_ultralightspeed import Client


class Local(Client):
    def __init__(self, **kwargs):
        super().__init__(key="offline", transport="threads", pack=8, **kwargs)
        self.sent = 0

    def ask(self, body):
        self.sent += 1
        answers = {}
        for name, text in body["state"].items():
            # Stable, nonconstant results make ordering mistakes observable.
            p = hashlib.sha256(text.encode()).digest()[0] / 255
            answers[name] = {"type": "noul", "noul": p}
        return {"answers": answers}


def measure_local(size, rounds, seed):
    if not 1 <= size <= 10000 or rounds < 1:
        raise ValueError("offline benchmark needs 1..10000 items and at least one round")
    items = [f"message {i}: café 東京" for i in range(size)]
    question = "Does this need attention?"
    criteria = {"true": "a current incident needs action", "false": "routine information"}
    rows = {name: [] for name in ("cold", "cache", "resume")}
    expected = None
    rng = random.Random(seed)
    # One untimed sample per shape, then randomized measured rounds.
    for turn in range(rounds + 1):
        order = list(rows)
        rng.shuffle(order)
        for name in order:
            with tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory) / "answers.jsonl" if name == "resume" else None
                if checkpoint is not None:
                    with Local(cache=False) as writer:
                        writer.classify(items, question, criteria=criteria, checkpoint=checkpoint)
                with Local(cache=name != "resume") as client:
                    if name == "cache":
                        client.classify(items, question, criteria=criteria)
                    sent = client.sent
                    started = time.perf_counter()
                    answers = client.classify(items, question, criteria=criteria,
                                              checkpoint=checkpoint)
                    elapsed = time.perf_counter() - started
                    # Compare every public answer field, not only the verdict.
                    actual = [asdict(answer) for answer in answers]
                    if expected is None:
                        expected = actual
                    if actual != expected:
                        raise AssertionError(f"{name}: answers differ from baseline")
                    if name != "cold" and client.sent != sent:
                        raise AssertionError(f"{name}: unexpectedly sent a request")
                    if turn:
                        rows[name].append(elapsed)
    print(f"offline: {size} items, {rounds} rounds; full answer agreement: 100%")
    print(f"{'shape':<10}{'p50 ms':>12}{'p95 ms':>12}{'items/s':>14}")
    for name, samples in rows.items():
        median = statistics.median(samples)
        tail = sorted(samples)[min(len(samples) - 1, int(0.95 * len(samples)))]
        print(f"{name:<10}{median * 1000:>12.2f}{tail * 1000:>12.2f}{size / median:>14.0f}")
    return 0
