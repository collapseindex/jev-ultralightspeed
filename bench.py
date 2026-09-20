"""
Reproduce the table in the README, against your own key.

    TYPESAFE_API_KEY=... python bench.py                    256 items, 3 rounds
    TYPESAFE_API_KEY=... python bench.py --items 1024
    TYPESAFE_API_KEY=... python bench.py --grid             batch x inflight

Every row classifies the same items with the same question, so the only thing
that changes is the shape of the requests. Three habits keep it honest:

  warm up first          a cold TLS handshake is not the thing being measured
  randomise the order    so provider drift cannot favour one shape
  report p95 as well     throughput can rise while the tail falls apart

The agreement column compares every answer with the sequential baseline. A
fast client that changes the verdicts is not a fast client, it is a bug.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time

sys.path.insert(0, "src")

from jev_ultralightspeed import Client                                   # noqa: E402

QUESTION = "Does this message need a human to act on it today?"
CRITERIA = {
    "true": "something is broken, blocked, at risk, or costing money right now",
    "false": "a question, a thank-you, feedback, or a request that can wait",
}

URGENT = [
    "Our payouts have been failing for {n} days and payroll is tomorrow.",
    "The export button does nothing on {browser}, my whole team is blocked.",
    "We were charged {n} times for the annual plan this morning.",
    "Production webhooks have failed since the deploy, {n} orders are stuck.",
    "All API calls return 500 since {n}:15 UTC.",
    "The data import silently dropped {n},000 rows last night.",
    "I cannot log in, the reset email never arrives, and a demo starts in {n} hour.",
    "Invoice {n}821 has the wrong VAT number and finance rejected it.",
]
CALM = [
    "Thanks for the quick fix yesterday, much appreciated.",
    "Do you have an office in {city}?",
    "Could you add dark mode at some point? No rush.",
    "What are your support hours over the holidays?",
    "Loving the new dashboard, great work.",
    "Is there a student discount for {city} universities?",
    "Please cancel my subscription at the end of the term.",
    "How do I change my profile picture on {browser}?",
]
BROWSERS = ["Safari", "Firefox", "Edge", "Chrome"]
CITIES = ["Berlin", "Lisbon", "Osaka", "Toronto"]


def corpus(size: int) -> list[str]:
    """Distinct items, so nothing is answered from a cache by accident."""
    items = []
    for index in range(size):
        pool = URGENT if index % 2 == 0 else CALM
        template = pool[(index // 2) % len(pool)]
        items.append(template.format(n=index % 9 + 1,
                                     browser=BROWSERS[index % len(BROWSERS)],
                                     city=CITIES[index % len(CITIES)]) + f" (ref {index:05d})")
    return items


class Timed(Client):
    """A client that remembers how long each request took."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.latencies: list[float] = []
        self.failures = 0

    def ask(self, body):
        started = time.monotonic()
        try:
            return super().ask(body)
        except Exception:
            self.failures += 1
            raise
        finally:
            self.latencies.append((time.monotonic() - started) * 1000)


def measure(items, *, pack, workers):
    client = Timed(pack=pack, workers=workers, cache=False)
    answers = client.classify(items, QUESTION, criteria=CRITERIA)
    return answers, client


def percentile(values, share):
    if not values:
        return 0.0
    ordered = sorted(values)
    at = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[at]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=256)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--grid", action="store_true", help="sweep pack x workers")
    parser.add_argument("--seed", type=int, default=7)
    arguments = parser.parse_args()

    items = corpus(arguments.items)
    random.seed(arguments.seed)

    shapes = ([(f"pack {p} x{w}", p, w) for p in (1, 2, 4, 8, 16, 32) for w in (1, 4, 8, 16)]
              if arguments.grid else
              [("sequential", 1, 1), ("concurrent x8", 1, 8), ("concurrent x16", 1, 16),
               ("packed 4 x4", 4, 4), ("packed 8 x4", 8, 4), ("packed 8 x8", 8, 8),
               ("packed 16 x8", 16, 8), ("packed 32 x8", 32, 8)])

    print(f"\n{len(items)} items, one yes-or-no question each, {arguments.rounds} rounds, "
          f"shapes in random order\n")

    # Warm up: one small call, so no row pays for the first handshake.
    measure(items[:4], pack=4, workers=1)

    baseline: dict[str, float] = {}
    rows = []
    order = [(name, pack, workers) for name, pack, workers in shapes for _ in range(arguments.rounds)]
    random.shuffle(order)
    collected: dict[str, list] = {}

    for name, pack, workers in order:
        answers, client = measure(items, pack=pack, workers=workers)
        if name == "sequential" and not baseline:
            baseline.update({answer.item: answer.p for answer in answers})
        collected.setdefault(name, []).append((answers, client))
        time.sleep(0.5)

    if not baseline:                       # the grid has no sequential row
        answers, _ = measure(items, pack=1, workers=1)
        baseline.update({answer.item: answer.p for answer in answers})

    print(f"{'shape':<16}{'items/s':>9}{'reqs':>7}{'tok/item':>10}{'p50 ms':>9}{'p95 ms':>9}"
          f"{'$/1k':>9}   agreement")
    for name, _, _ in shapes:
        runs = collected.get(name) or []
        if not runs:
            continue
        rates = [client.usage.items_per_second for _, client in runs]
        tokens = statistics.mean(client.usage.tokens_per_item for _, client in runs)
        requests = statistics.median(client.usage.requests for _, client in runs)
        latencies = [value for _, client in runs for value in client.latencies]
        failures = sum(client.failures for _, client in runs)
        same, moved = [], []
        for answers, _ in runs:
            checked = [a for a in answers if a.item in baseline]
            if not checked:
                continue
            same.append(sum(1 for a in checked if (a.p >= 0.5) == (baseline[a.item] >= 0.5)) / len(checked))
            moved.append(statistics.mean(abs(a.p - baseline[a.item]) for a in checked))
        agreement = (f"{statistics.mean(same) * 100:.1f}% same, move {statistics.mean(moved):.3f}"
                     if same else "baseline")
        if failures:
            agreement += f", {failures} failed"
        rows.append((name, statistics.median(rates)))
        print(f"{name:<16}{statistics.median(rates):>9.1f}{requests:>7.0f}{tokens:>10.0f}"
              f"{percentile(latencies, 0.5):>9.0f}{percentile(latencies, 0.95):>9.0f}"
              f"{tokens * 1000 * 0.042 / 1e6:>9.4f}   {agreement}")

    slowest = min(rate for _, rate in rows)
    best, fastest = max(rows, key=lambda row: row[1])
    print(f"\nfastest: {best} at {fastest:.1f} items/s, {fastest / slowest:.0f}x the slowest row\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
