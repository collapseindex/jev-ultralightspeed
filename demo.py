"""
Two clients, one ceiling, thirty seconds. Watch the gap open.

    python demo.py                      # replayed from the recorded run, no key
    TYPESAFE_API_KEY=... python demo.py --live

The point is the delta, not the rate. One request per item and thirty-two items a
request are both held to the same 1,000 requests a minute, so the only thing that
differs is how many judgements ride on each one. After thirty seconds the counters
are about thirty-two apart, and the footer says what that means for a million rows.

Replay mode uses the real timings from `bench_eval.py`'s run, which is in
`data/results`: 30,000 requests in 1,801 seconds against 940. Nothing is invented,
it is the same numbers at a different speed. `--live` runs both arms against the
API for about five cents and is slower to start.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, "src")

CEILING = 1_000                  # requests a minute, the client's default
PACK = 32
SECONDS = 30
BAR = 34


def _bars() -> tuple[str, str]:
    """Blocks where the terminal can take them, hashes where it cannot."""
    try:
        "\u2588\u2591".encode(sys.stdout.encoding or "utf-8")
        return "\u2588", "\u2591"
    except (UnicodeEncodeError, LookupError, TypeError):
        return "#", "."


GREY, BLUE, WHITE, DIM, OFF = "\033[38;5;244m", "\033[38;5;39m", "\033[97m", "\033[2m", "\033[0m"


FULL, EMPTY = _bars()


def a_million(rate: float) -> str:
    """How long a million judgements take at this rate, in words a person uses."""
    hours = 1_000_000 / rate / 3600
    if hours >= 1.5:
        return f"{hours:.0f} hours"
    return f"{hours * 60:.0f} minutes"


def draw(elapsed: float, counts: dict[str, float], note: str) -> None:
    width = min(shutil.get_terminal_size((90, 20)).columns, 96)
    lines = [f"{WHITE}30,000 judgements, one ceiling of {CEILING} requests a minute{OFF}", ""]
    most = max(max(counts.values()), 1.0)
    for name, done in counts.items():
        filled = int(BAR * min(1.0, done / most))
        colour = BLUE if "ultralightspeed" in name else GREY
        bar = colour + FULL * filled + DIM + EMPTY * (BAR - filled) + OFF
        rate = done / elapsed if elapsed else 0.0
        lines.append(f"  {name:<22} {bar} {done:>8,.0f}  {rate:>6.0f}/s")
        lines.append(f"  {DIM}{'':<22} {'a million would take ' + a_million(max(rate, 1)):<44}{OFF}")
    lines += ["", f"  {DIM}{note}{OFF}", f"  {DIM}{elapsed:>4.1f}s elapsed{OFF}"]
    sys.stdout.write("\033[H\033[J" + "\n".join(line[:width] for line in lines) + "\n")
    sys.stdout.flush()


def replay() -> None:
    """The recorded run, sped up. Both rates are what the benchmark measured."""
    plain = 30_000 / 1801.3                       # the unpacked arm, 16.7 a second
    packed = PACK * CEILING / 60                  # what the ceiling allows a packed arm
    counts = {"one request per item": 0.0, "jev + ultralightspeed": 0.0}
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed > SECONDS:
            break
        counts["one request per item"] = min(30_000, plain * elapsed)
        counts["jev + ultralightspeed"] = min(30_000, packed * elapsed)
        draw(elapsed, counts, "replayed from data/results at 1x. Nothing here is invented.")
        time.sleep(0.08)
    finish(counts, plain, packed)


def live() -> None:
    """Both arms against the real API, sharing one ceiling, for about five cents."""
    from jev_ultralightspeed import Client, REQUESTS_PER_MINUTE, _Limiter

    rows = [f"ticket {n}: the checkout is failing and orders are being lost" for n in range(40_000)]
    question = "Does this need a human today?"
    shared = _Limiter(REQUESTS_PER_MINUTE)
    counts = {"one request per item": 0.0, "jev + ultralightspeed": 0.0}
    stop = threading.Event()

    def run(name: str, pack: int, feed) -> None:
        client = Client(pack=pack, workers=8, cache=False, dedupe=False, limiter=shared)
        client.warm()
        try:
            for _ in client.stream(feed, question, chunk=2_000):
                counts[name] += 1
                if stop.is_set():
                    return
        except Exception:                          # the demo ends, it does not crash
            return
        finally:
            client.close()

    threads = [threading.Thread(target=run, args=("one request per item", 1, iter(rows[:2_000])),
                                daemon=True),
               threading.Thread(target=run, args=("jev + ultralightspeed", PACK,
                                                  iter(rows)), daemon=True)]
    for thread in threads:
        thread.start()
    started = time.monotonic()
    while time.monotonic() - started < SECONDS:
        elapsed = time.monotonic() - started
        draw(elapsed, counts, "live, both arms sharing one ceiling. About five cents.")
        time.sleep(0.08)
    stop.set()
    elapsed = time.monotonic() - started
    finish(counts, counts["one request per item"] / elapsed,
           counts["jev + ultralightspeed"] / elapsed)


def finish(counts: dict[str, float], plain: float, packed: float) -> None:
    draw(SECONDS, counts, "done")
    print()
    print(f"  {WHITE}In {SECONDS} seconds: {counts['jev + ultralightspeed']:,.0f} judgements "
          f"against {counts['one request per item']:,.0f}.{OFF}")
    print(f"  {WHITE}A million: {a_million(packed)} against {a_million(plain)}.{OFF}")
    print(f"  {DIM}Same ceiling, same model, same question. "
          f"{PACK} items on a request instead of 1.{OFF}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="run both arms against the API, about five cents")
    arguments = parser.parse_args()
    if os.name == "nt":
        os.system("")                              # let Windows terminals do colour
    try:                                           # and let them take the block characters
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    global FULL, EMPTY
    FULL, EMPTY = _bars()
    try:
        live() if arguments.live else replay()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
