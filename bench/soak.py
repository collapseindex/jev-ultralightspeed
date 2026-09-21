"""
A long run, the way the tool is meant to be used: a queue of items, one
question, answers streamed out to a file.

    TYPESAFE_API_KEY=... python soak.py --items 100000
    TYPESAFE_API_KEY=... python soak.py --items 100000 --out answers.csv

Nothing is held in memory: items are generated one at a time and answers are
written as they land, so the memory footprint is the chunk size, not the job.
Progress, throughput, cost and the error count are printed as it goes.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

# Anchored to the file, not to where you happen to be standing.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_ultralightspeed import Client  # noqa: E402

QUESTION = "Does this message need a human to act on it today?"
CRITERIA = {
    "true": "something is broken, blocked, at risk, or costing money right now",
    "false": "a question, a thank-you, feedback, or a request that can wait",
}

URGENT = [
    "Our {thing} has been failing for {n} days and {deadline} is tomorrow.",
    "The {thing} does nothing on {browser}, my whole team is blocked.",
    "We were charged {n} times for the {plan} plan this morning.",
    "Production {thing} has failed since the deploy, {n} orders are stuck.",
    "All API calls return 500 since {n}:15 UTC, {city} office cannot work.",
    "The data import silently dropped {n},000 rows from {thing} last night.",
    "I cannot log in, the reset email never arrives, and {deadline} starts in {n} hour.",
    "Invoice {n}821 has the wrong VAT number and finance rejected it before {deadline}.",
]
CALM = [
    "Thanks for the quick fix to the {thing} yesterday, much appreciated.",
    "Do you have an office in {city}?",
    "Could you add dark mode to the {thing} at some point? No rush.",
    "What are your support hours in {city} over the holidays?",
    "Loving the new {thing}, great work.",
    "Is there a student discount for {city} universities on the {plan} plan?",
    "Please cancel my {plan} subscription at the end of the term.",
    "How do I change my profile picture on {browser}?",
]
THINGS = ["payout run", "export button", "webhook", "search index", "billing page", "dashboard",
          "CSV import", "mobile app", "audit log", "invoice PDF", "SSO login", "report builder"]
BROWSERS = ["Safari", "Firefox", "Edge", "Chrome", "Brave", "Arc"]
CITIES = ["Berlin", "Lisbon", "Osaka", "Toronto", "Nairobi", "Lima", "Oslo", "Manila"]
PLANS = ["Pro", "Team", "Enterprise", "Starter"]
DEADLINES = ["payroll", "the board demo", "the audit", "the launch", "month end"]


def queue(size: int, seed: int = 11):
    """A generator, not a list: the job never exists in memory all at once."""
    random.seed(seed)
    for index in range(size):
        pool = URGENT if index % 2 == 0 else CALM
        template = random.choice(pool)
        yield template.format(
            n=random.randint(1, 9), thing=random.choice(THINGS), browser=random.choice(BROWSERS),
            city=random.choice(CITIES), plan=random.choice(PLANS),
            deadline=random.choice(DEADLINES)) + f" (ticket {index:07d})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=100_000)
    parser.add_argument("--pack", type=int, default=32)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk", type=int, default=5_000)
    parser.add_argument("--out", default="")
    parser.add_argument("--checkpoint", default="",
                        help="append answers here and skip what is already in it; "
                             "kill this run and start it again to see it work")
    arguments = parser.parse_args()

    client = Client(pack=arguments.pack, workers=arguments.workers, cache=False)
    client.warm()
    print(f"\n{arguments.items:,} items, pack {arguments.pack}, {arguments.workers} in flight, "
          f"transport {client.transport}, chunks of {arguments.chunk:,}\n")

    writer = None
    handle = open(arguments.out, "w", newline="", encoding="utf-8") if arguments.out else None
    if handle:
        writer = csv.writer(handle)
        writer.writerow(["item", "label", "p"])

    counts: Counter[str] = Counter()
    started = time.monotonic()
    last = started
    done = 0
    try:
        for answer in client.stream(queue(arguments.items), QUESTION,
                                    criteria=CRITERIA, chunk=arguments.chunk,
                                    checkpoint=arguments.checkpoint or None):
            counts[answer.label] += 1
            done += 1
            if writer:
                writer.writerow([answer.item, answer.label, f"{answer.p:.4f}"])
            now = time.monotonic()
            if now - last >= 5.0:
                rate = done / (now - started)
                left = (arguments.items - done) / rate if rate else 0
                print(f"  {done:>8,} of {arguments.items:,}  {rate:>7.1f} items/s  "
                      f"${client.usage.usd:>7.3f}  {left / 60:>5.1f} min left", flush=True)
                last = now
    finally:
        if handle:
            handle.close()
        client.close()

    seconds = time.monotonic() - started
    print(f"\n{done:,} items in {seconds / 60:.1f} min at {done / seconds:.1f} items/s")
    print(f"{client.usage.requests:,} requests, {client.usage.retries:,} retried, "
          f"{client.usage.tokens_per_item:.0f} tokens/item, "
          f"${client.usage.usd:.2f} total, ${client.usage.usd / done * 1e6:.2f} per million items")
    print(f"labels: {dict(counts)}")
    if arguments.out:
        print(f"written to {arguments.out} ({os.path.getsize(arguments.out) / 1e6:.1f} MB)")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
