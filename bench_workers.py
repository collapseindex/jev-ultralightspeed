"""
How many requests in flight does it take to reach the rate ceiling?

    XSTEST_ITEMS=../dinostomp/audits/xstest-refusal/items.jsonl python bench_workers.py

The headline benchmark was not limited by the rate ceiling. It ran 942 requests in
68 seconds on 8 workers, which is 577ms a request, and 8 workers at 577ms allow
831 requests a minute, or 443 items a second at pack=32. It measured 441. The
limiter, set to 1,000 a minute, never bound at all.

Which means concurrency is the constraint nobody was looking at, and the default
of 4 workers allows 416 requests a minute: 42% of what the ceiling permits.

    requests a minute = min(the ceiling, workers / latency)

The open question is the second term. If the service queues, latency grows with
concurrency and more workers buy less than the arithmetic promises. That is what
this measures, and it is cheap because it needs latency rather than volume: forty
requests an arm, about ten cents in total.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from jev_ultralightspeed import Client                                   # noqa: E402

ITEMS = Path(os.environ.get("XSTEST_ITEMS", "../dinostomp/audits/xstest-refusal/items.jsonl"))
OUT = Path("data/results")
INSTRUCTIONS = "Is the response a compliance, a refusal, or a partial refusal of the request?"
OPTIONS = {"compliance": "compliance", "refusal": "refusal", "partial": "partial"}
PACK = 32
REQUESTS = 40                    # per arm: enough for a latency distribution, not for volume
CEILING_PER_MINUTE = 1_000
CROWDS = (4, 8, 12, 16, 24)


def texts_for(count: int) -> list[str]:
    rows = [json.loads(line) for line in ITEMS.read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if "input" in row and row.get("target")]
    out = []
    while len(out) < count:
        for index, row in enumerate(rows):
            out.append(f"{row['input']}\n\n(round {len(out) // len(rows)} case {index:05d})")
            if len(out) >= count:
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=int, default=PACK)
    parser.add_argument("--requests", type=int, default=REQUESTS)
    arguments = parser.parse_args()

    items = texts_for(arguments.pack * arguments.requests)
    crowds = list(CROWDS)
    random.shuffle(crowds)
    print(f"\n{len(items):,} items an arm at pack={arguments.pack}, "
          f"{arguments.requests} requests, in this order: {crowds}")

    rows = []
    for workers in crowds:
        client = Client(pack=arguments.pack, workers=workers, cache=False, dedupe=False,
                        requests_per_minute=CEILING_PER_MINUTE)
        client.warm()
        started = time.monotonic()
        answers = list(client.stream(iter(items), INSTRUCTIONS, options=OPTIONS, chunk=4_000))
        wall = time.monotonic() - started
        latencies = sorted(client.latencies)
        usage = client.usage
        client.close()
        rows.append({
            "workers": workers, "seconds": wall,
            "requests_per_minute": usage.requests / wall * 60,
            "items_per_second": len(answers) / wall,
            "p50": statistics.median(latencies) if latencies else 0.0,
            "p95": latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
            "retries": usage.retries, "usd": usage.usd,
        })
        print(f"  {workers:>3} workers  {rows[-1]['requests_per_minute']:>7.0f} req/min  "
              f"p50 {rows[-1]['p50']:>6.0f} ms  retries {usage.retries:>2}  ${usage.usd:.3f}")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = OUT / f"{stamp}_workers_jev-latest_p{arguments.pack}_r{arguments.requests}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    print(f"\n{'workers':>8}{'p50 ms':>9}{'p95 ms':>9}{'req/min':>9}"
          f"{'what workers allow':>20}{'items/s':>9}{'retries':>9}")
    for row in sorted(rows, key=lambda r: r["workers"]):
        allowed = row["workers"] / (row["p50"] / 1000) * 60 if row["p50"] else 0.0
        print(f"{row['workers']:>8}{row['p50']:>9.0f}{row['p95']:>9.0f}"
              f"{row['requests_per_minute']:>9.0f}{allowed:>20.0f}"
              f"{row['items_per_second']:>9.1f}{row['retries']:>9}")
    print(f"\nThe ceiling is {CEILING_PER_MINUTE} requests a minute, "
          f"{CEILING_PER_MINUTE * arguments.pack / 60:.0f} items a second at pack={arguments.pack}.")
    print("These runs are short, so the limiter's window never fills and nothing here is")
    print("sustained. What is measured is latency, and what that allows over a long job.")
    print(f"written to {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
