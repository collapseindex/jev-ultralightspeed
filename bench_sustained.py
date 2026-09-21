"""
Is the throughput a rate, or a burst that has not run out yet?

    python bench_sustained.py --minutes 10 --budget 2.50
    python bench_sustained.py --analyse data/results/<the file it wrote>

Every tuning run in this repository so far finished inside a minute, and a minute
is the width of the limiter's own window. A short run spends its whole life
draining a bucket that started full, so the requests-a-minute it reports is a
burst and not a rate. Two numbers were published off runs like that and both were
withdrawn. This one runs until the window has refilled many times over and the
client is living on whatever the far end will actually sustain.

It measures one arm, the shipping shape, because there is nothing to compare: the
question is not which setting is faster, it is whether the number holds up when
the run is longer than the thing that limits it.

What it reports is a series, not an average. A rate that decays is the finding.

The reading is taken by sampling `client.usage` from a watcher thread. Those are
plain integer fields being incremented by the workers, so a sample can sit a few
requests behind, which does not matter across a sixty second window and is the
reason no field is read twice in the same calculation.

It stops at whichever comes first, the clock or the budget, and the budget is
enforced by the generator feeding the run simply stopping. Nothing is cancelled
mid-flight, so the last requests land and are counted.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "src")

from jev_ultralightspeed import Client  # noqa: E402
from jev_ultralightspeed._limits import _Limiter  # noqa: E402


class Watched(_Limiter):
    """
    The limiter the client would use anyway, keeping roughly when each permit went.

    Roughly, and the word is load bearing. The stamp here is taken after the
    permit was granted, so it sits a little later than the one the limiter
    actually counted on, by however long a lock release takes under eight
    threads. That is microseconds, which says nothing about a shape drawn in
    minutes and everything about whether exactly one thousand or one thousand and
    one fell inside a window.

    So this is for the shape. Whether the ceiling holds is settled in the test
    suite, against the limiter's own stamps, where the question has an exact
    answer and no API is needed to ask it.

    `list.append` across workers is safe.
    """

    def __init__(self, per_minute: int, *, paced: bool = False) -> None:
        super().__init__(per_minute, paced=paced)
        self.admitted: list[float] = []

    def try_take(self) -> float:
        wait = super().try_take()
        if wait <= 0.0:
            self.admitted.append(time.monotonic())
        return wait

OUT = Path("data/results")
PUBLISHED_PER_MINUTE = 1_200     # what TypeSafe documents
PUBLISHED_TOKENS_PER_SECOND = 250_000
INSTRUCTIONS = "Does this message need a human to look at it?"
SAMPLE_SECONDS = 0.5
WINDOW = 60.0                    # the limiter's window, so the unit the claim is in
# How far over the ceiling the admission stamps may read before it means anything.
# They are taken just after each permit rather than at it, and under eight threads
# that jitter is enough to move a request across a window edge. One, not a budget.
TIMING_SLOP = 1

# Enough shapes that no two neighbouring items are identical, because dedupe is
# off and identical text would still be a different measurement than real work.
SUBJECTS = ("the invoice", "my account", "the shipping estimate", "a refund",
            "the login page", "your support line", "the renewal", "an order")
TROUBLES = ("has been wrong twice", "will not load", "arrived late", "was charged twice",
            "says the card expired", "never sent a confirmation", "shows the old address",
            "timed out again this morning")


def messages(deadline: float, budget: float, client: Client, note: dict):
    """
    An endless supply that stops itself.

    A generator rather than a list: the run is hundreds of thousands of items and
    there is no reason for any of them to be in memory after they are asked. It
    also gives the run a clean way to end. Returning from here ends the stream,
    the requests already in flight land and get counted, and nothing has to be
    cancelled.
    """
    index = 0
    while True:
        if time.monotonic() >= deadline:
            note["stopped"] = "the clock"
            return
        if client.usage.usd >= budget:
            note["stopped"] = "the budget"
            return
        subject, trouble = SUBJECTS[index % len(SUBJECTS)], TROUBLES[index // 7 % len(TROUBLES)]
        yield (f"Ticket {index:07d}. Hello, I am writing because {subject} {trouble}. "
               f"I have tried the obvious things and it is still happening. This is the "
               f"{index % 5 + 1} time I have had to get in touch about it and I would like "
               f"to know what you are going to do. Reference {index * 7919 % 1000000:06d}.")
        index += 1


def watch(client: Client, samples: list, stop: threading.Event) -> None:
    """A reading of the counters every half second, for the windows afterwards."""
    while not stop.is_set():
        usage = client.usage
        samples.append({"t": time.monotonic(), "requests": usage.requests,
                        "items": usage.items, "input_tokens": usage.input_tokens,
                        "retries": usage.retries,
                        # pushback is a count per status code, and only the total
                        # is a rate; the breakdown is read off the final usage.
                        "pushback": sum(usage.pushback.values()),
                        "waited": usage.waited})
        stop.wait(SAMPLE_SECONDS)


def collect(arguments) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = OUT / (f"{stamp}_sustained_jev-latest_{arguments.minutes}min"
                  f"_p{arguments.pack}_w{arguments.workers}_r{arguments.rpm}_s0.jsonl")

    limiter = Watched(arguments.rpm, paced=arguments.paced)
    client = Client(pack=arguments.pack, workers=arguments.workers,
                    requests_per_minute=arguments.rpm, paced=arguments.paced,
                    cache=False, dedupe=False, limiter=limiter)
    client.warm()

    samples: list[dict] = []
    stop = threading.Event()
    note: dict = {"stopped": "the stream ran out"}
    watcher = threading.Thread(target=watch, args=(client, samples, stop), daemon=True)

    print(f"{arguments.minutes} minutes at pack={arguments.pack}, workers={arguments.workers}, "
          f"ceiling={arguments.rpm}/min, paced={arguments.paced}, budget ${arguments.budget:.2f}.")
    print("A minute is the limiter's window, so the first one is the burst and the rest "
          "are the rate.\n")

    began = time.monotonic()
    deadline = began + arguments.minutes * 60
    watcher.start()
    answered = 0
    try:
        for _ in client.stream(messages(deadline, arguments.budget, client, note),
                               INSTRUCTIONS, chunk=20_000, on_error="skip"):
            answered += 1
            if answered % 20_000 == 0:
                gone = time.monotonic() - began
                print(f"  {gone / 60:>4.1f} min  {answered:>8,} items  "
                      f"{client.usage.requests / (gone / 60):>7,.0f} req/min  "
                      f"${client.usage.usd:.2f}")
    finally:
        stop.set()
        watcher.join(timeout=2)
        usage = client.usage
        client.close()

    ran = time.monotonic() - began
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "jev-sustained-run", "when": stamp, "seconds": ran,
            "pack": arguments.pack, "workers": arguments.workers, "rpm": arguments.rpm,
            "paced": arguments.paced, "stopped_by": note["stopped"],
            "usage": {field: getattr(usage, field) for field in usage.__dataclass_fields__},
            "usd": usage.usd, "answered": answered, "failures": len(client.failures),
            # When each request was let out, not when it came back. The ceiling
            # is a promise about the first and only the first.
            "admitted": [round(stamp - began, 4) for stamp in sorted(limiter.admitted)],
        }) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample) + "\n")
    print(f"\nstopped by {note['stopped']} after {ran / 60:.1f} minutes, ${usage.usd:.2f} spent")
    print(f"{len(samples):,} readings written to {path}")
    return path


# -- reading it back ---------------------------------------------------------

def read(path: Path) -> tuple[dict, list[dict]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    samples = [json.loads(line) for line in lines[1:] if line]
    for sample in samples:
        # The first runs of this bench stored pushback as the per-status mapping
        # it is on Usage. Only the total is a rate, and an older file should not
        # stop being readable over it.
        if isinstance(sample.get("pushback"), dict):
            sample["pushback"] = sum(sample["pushback"].values())
    return json.loads(lines[0]), samples


def worst_window(admitted: list[float], width: float) -> tuple[int, float]:
    """
    The most permits the limiter ever granted inside one window, and when.

    This is the only check that the ceiling holds under load rather than in a
    unit test: it is a promise about every window, not about the average, and an
    average can sit under it while a window it is made of does not. Sliding, not
    anchored, because a breach does not wait for a minute boundary: nine hundred
    in the back half of one minute and nine hundred in the front half of the next
    is obedient by the clock and eighteen hundred in the sixty seconds the server
    saw.

    Two pointers over sorted stamps, so ten thousand of them is ten thousand
    steps rather than the square of it.
    """
    if not admitted:
        return 0, 0.0
    stamps = sorted(admitted)
    worst, began, start = 0, 0.0, 0
    for end, moment in enumerate(stamps):
        while moment - stamps[start] > width:
            start += 1
        if end - start + 1 > worst:
            worst, began = end - start + 1, stamps[start] - stamps[0]
    return worst, began


def windows(samples: list[dict], width: float, pack: int) -> list[dict]:
    """
    Every full `width` of wall clock, as a rate.

    Non-overlapping and anchored at the start, so each row is a period the run
    actually lived through rather than a smoothing of its neighbours. A rolling
    window would hide exactly the thing this is looking for.
    """
    if len(samples) < 2:
        return []
    start = samples[0]["t"]
    rows, edge, previous = [], start + width, samples[0]
    for sample in samples:
        if sample["t"] < edge:
            continue
        gap = sample["t"] - previous["t"]
        if gap > 0:
            rows.append({
                "from": previous["t"] - start,
                "seconds": gap,
                "requests_per_minute": (sample["requests"] - previous["requests"]) / gap * 60,
                # From requests rather than usage.items, which moves a whole chunk
                # at a time and would make a smooth rate look like a staircase.
                "items_per_second":
                    (sample["requests"] - previous["requests"]) * pack / gap,
                "tokens_per_second":
                    (sample["input_tokens"] - previous["input_tokens"]) / gap,
                "retries": sample["retries"] - previous["retries"],
                "pushback": sample["pushback"] - previous["pushback"],
                "waited": sample["waited"] - previous["waited"],
            })
        previous, edge = sample, edge + width
    return rows


def analyse(path: Path) -> None:
    head, samples = read(path)
    rows = windows(samples, WINDOW, head["pack"])
    usage = head["usage"]
    print(f"\n{head['answered']:,} items in {head['seconds'] / 60:.1f} minutes, "
          f"from {path.name}")
    print(f"pack={head['pack']}, workers={head['workers']}, ceiling={head['rpm']}/min, "
          f"paced={head['paced']}, stopped by {head['stopped_by']}")

    if not rows:
        print("\nthe run was shorter than one window, which is the thing this bench exists "
              "to avoid. Nothing to report.")
        return

    print(f"\nminute by minute, each one a full {WINDOW:.0f} seconds of its own")
    print(f"  {'from':>6}  {'req/min':>8}  {'of ceiling':>10}  {'items/s':>8}  "
          f"{'tokens/s':>9}  {'retries':>7}  {'429s':>5}  {'waited':>7}")
    for row in rows:
        print(f"  {row['from'] / 60:>5.0f}m  {row['requests_per_minute']:>8,.0f}  "
              f"{row['requests_per_minute'] / head['rpm']:>9.0%}  "
              f"{row['items_per_second']:>8,.0f}  {row['tokens_per_second']:>9,.0f}  "
              f"{row['retries']:>7,}  {row['pushback']:>5,}  {row['waited']:>6.1f}s")

    first, rest = rows[0], rows[1:]
    print(f"\nthe first window is the burst: {first['requests_per_minute']:,.0f} requests a "
          f"minute, {first['requests_per_minute'] / head['rpm']:.0%} of the ceiling.")
    if not rest:
        print("There was no second window, so this run still cannot tell a burst from a rate.")
        return

    rates = [row["requests_per_minute"] for row in rest]
    items = [row["items_per_second"] for row in rest]
    held = statistics.mean(rates)
    print(f"The {len(rest)} after it are the rate: {held:,.0f} requests a minute on average "
          f"({min(rates):,.0f} to {max(rates):,.0f}), {held / head['rpm']:.0%} of the ceiling.")
    print(f"That is {statistics.mean(items):,.0f} items a second at pack={head['pack']}, "
          f"against {head['rpm'] * head['pack'] / 60:,.0f} if the ceiling were reached exactly.")

    # Whether it decayed. Half against half rather than first against last, because
    # one slow window is noise and this claim should not turn on it.
    if len(rest) >= 4:
        half = len(rest) // 2
        early, late = statistics.mean(rates[:half]), statistics.mean(rates[half:])
        drift = (late - early) / early
        print(f"\nfront half {early:,.0f} a minute, back half {late:,.0f}, "
              f"a drift of {drift:+.1%}.")
        print("  " + ("Flat within the noise, so the rate is a rate." if abs(drift) < 0.05
                      else "That is a decay, and the sustained number is the back half, "
                           "not the average."))

    tokens = statistics.mean(row["tokens_per_second"] for row in rest)
    print(f"\ntokens a second {tokens:,.0f}, which is {tokens / PUBLISHED_TOKENS_PER_SECOND:.1%} "
          f"of the published {PUBLISHED_TOKENS_PER_SECOND:,}. The request ceiling binds first, "
          f"and by a long way, which is the whole reason packing pays.")
    admitted = head.get("admitted")
    if admitted:
        worst, began = worst_window(admitted, WINDOW)
        over = worst - head["rpm"]
        print(f"\nwhen requests actually went out: of the {len(admitted):,} let out, the fullest "
              f"{WINDOW:.0f} seconds anywhere in the run held {worst:,} against a ceiling of "
              f"{head['rpm']:,}, starting {began / 60:.1f} minutes in.")
        if over <= 0:
            print("  Under it in every window there was.")
        elif over <= TIMING_SLOP:
            print(f"  {over} over, which is inside what this measurement can resolve: the stamp "
                  f"is taken just after the permit, not at it. The ceiling is proved in the "
                  f"test suite against the limiter's own stamps, not here.")
        else:
            print(f"  OVER by {over:,}, which is too many to blame on timing. Worth a look.")

    # Status 0 is the library's mark for an attempt that never got an answer at
    # all, so it is a dropped connection and not the server pushing back. Adding
    # the two together would name the wrong thing as the cause.
    pushback = {int(status): count for status, count in usage["pushback"].items()}
    dropped = pushback.pop(0, 0)
    detail = [f"{count:,} x {status}" for status, count in sorted(pushback.items())]
    if dropped:
        detail.append(f"{dropped:,} with no answer at all")
    print(f"\n{usage['retries']:,} retries over {usage['requests']:,} requests"
          f"{' (' + ', '.join(detail) + ')' if detail else ''}: "
          f"{sum(pushback.values()) / max(1, usage['requests']):.2%} of the requests were "
          f"pushed back, {dropped / max(1, usage['requests']):.2%} dropped.")
    print(f"{usage['waited']:.0f} seconds went on retry backoff. Time spent held by the "
          f"limiter does not appear here and is the flat stretch in the table above.")
    print(f"{head['failures']:,} items came back unusable. ${head['usd']:.2f} spent.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyse", default="", help="a file from an earlier run; costs nothing")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--budget", type=float, default=2.50, help="USD, a hard stop")
    parser.add_argument("--pack", type=int, default=32)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rpm", type=int, default=1_000, help="the client's default ceiling")
    parser.add_argument("--paced", action="store_true")
    arguments = parser.parse_args()
    analyse(Path(arguments.analyse) if arguments.analyse else collect(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
