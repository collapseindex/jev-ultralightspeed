"""
Two clients, one ceiling. Watch the gap open.

    python demo.py                      # replayed from the recorded run, no key
    python demo.py --items 10000        # a shorter one
    TYPESAFE_API_KEY=... python demo.py --live

Both arms are held to the same 1,000 requests a minute, so the only thing that
differs is how many judgements ride on each request. It runs until the packed arm
has finished the job, and the other bar shows how far it got in the same time.

Replay uses the rates the benchmark measured: 16.7 items a second one at a time,
and what the ceiling allows at pack=32. Nothing is invented. `--live` runs both
arms against the API, sharing one limiter, for about ten cents.
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
ITEMS = 30_000
BAR = 24
PLAIN, PACKED = "regular jev", "ultralightspeed jev"
GREY, BLUE, WHITE, DIM, OFF = "\033[38;5;244m", "\033[38;5;45m", "\033[97m", "\033[2m", "\033[0m"

# Padded to one width so the speed lines behind him line up.
SANIC = [line.ljust(15) for line in [
    "       ___",
    "     ,'   `.",
    "    /  o  o \\",
    "   |    >    |",
    "    \\  \\__/ /",
    "     `.___,'",
    "     //   \\\\",
    "    ''     ''",
]]


def _bars() -> tuple[str, str]:
    """Blocks where the terminal can take them, hashes where it cannot."""
    try:
        "█░".encode(sys.stdout.encoding or "utf-8")
        return "█", "░"
    except (UnicodeEncodeError, LookupError, TypeError):
        return "#", "."


FULL, EMPTY = _bars()


def a_million(rate: float) -> str:
    """How long a million judgements take at this rate, in words a person uses."""
    if rate <= 0:
        return "forever"
    hours = 1_000_000 / rate / 3600
    return f"{hours:.0f} hours" if hours >= 1.5 else f"{hours * 60:.0f} minutes"


def draw(elapsed: float, counts: dict[str, float], target: int, note: str) -> None:
    width = min(shutil.get_terminal_size((100, 30)).columns, 100)
    out = [f"{WHITE}{target:,} judgements, one ceiling of {CEILING} requests a minute{OFF}", ""]
    speed = counts[PACKED] / max(elapsed, 0.1)
    # The trail streams out behind him, which is the left, and it grows with the
    # packed arm's rate, so the picture is a gauge rather than a decoration.
    length = min(22, int(speed / 24))
    for index, line in enumerate(SANIC):
        trail = ("=" * length) if 2 <= index <= 5 else ""
        out.append(f"  {BLUE}{trail:>22}{OFF}{DIM}{line}{OFF}")
    out.append("")
    for name in (PLAIN, PACKED):
        done = counts[name]
        share = min(1.0, done / target)
        filled = round(BAR * share)
        colour = BLUE if name == PACKED else GREY
        bar = colour + FULL * filled + DIM + EMPTY * (BAR - filled) + OFF
        rate = done / elapsed if elapsed else 0.0
        out.append(f"  {WHITE}{name:<21}{OFF}{bar} {done:>7,.0f} {rate:>6.0f}/s {share:>5.0%}")
        out.append(f"  {DIM}{'':<21}a million would take {a_million(rate)}{OFF}")
    out += ["", f"  {DIM}{note}{OFF}", f"  {DIM}{elapsed:>5.1f}s elapsed{OFF}"]
    sys.stdout.write("\033[H\033[J" + "\n".join(out) + "\n")
    sys.stdout.flush()


def replay(target: int) -> None:
    """The recorded run. Both rates are what the benchmark measured."""
    plain = 30_000 / 1801.3                       # the unpacked arm, 16.7 a second
    packed = PACK * CEILING / 60                  # what the ceiling allows at pack=32
    counts = {PLAIN: 0.0, PACKED: 0.0}
    started = time.monotonic()
    while counts[PACKED] < target:
        elapsed = time.monotonic() - started
        counts[PLAIN] = min(target, plain * elapsed)
        counts[PACKED] = min(target, packed * elapsed)
        draw(elapsed, counts, target, "replayed from data/results. Nothing here is invented.")
        time.sleep(0.08)
    finish(counts, target, time.monotonic() - started)


def live(target: int) -> None:
    """Both arms against the real API, sharing one ceiling."""
    from jev_ultralightspeed import Client, REQUESTS_PER_MINUTE, _Limiter

    rows = [f"ticket {n}: the checkout is failing and orders are being lost" for n in range(target)]
    question = "Does this need a human today?"
    shared = _Limiter(REQUESTS_PER_MINUTE)
    counts = {PLAIN: 0.0, PACKED: 0.0}
    stop = threading.Event()

    def run(name: str, pack: int) -> None:
        client = Client(pack=pack, workers=8, cache=False, dedupe=False, limiter=shared)
        client.warm()
        try:
            for _ in client.stream(iter(rows), question, chunk=2_000):
                counts[name] += 1
                if stop.is_set():
                    return
        except Exception:                          # the demo ends, it does not crash
            return
        finally:
            client.close()

    for name, pack in ((PLAIN, 1), (PACKED, PACK)):
        threading.Thread(target=run, args=(name, pack), daemon=True).start()
    started = time.monotonic()
    while counts[PACKED] < target:
        draw(time.monotonic() - started, counts, target, "live, both arms sharing one ceiling.")
        time.sleep(0.08)
    stop.set()
    finish(counts, target, time.monotonic() - started)


def finish(counts: dict[str, float], target: int, elapsed: float) -> None:
    draw(elapsed, counts, target, "done")
    behind = counts[PLAIN]
    print()
    print(f"  {WHITE}{target:,} judgements in {elapsed:.0f} seconds. "
          f"regular jev got through {behind:,.0f}.{OFF}")
    print(f"  {WHITE}A million: {a_million(counts[PACKED] / elapsed)} against "
          f"{a_million(behind / elapsed)}.{OFF}")
    print(f"  {DIM}Same ceiling, same model, same question. "
          f"{PACK} items on a request instead of 1.{OFF}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=ITEMS)
    parser.add_argument("--live", action="store_true",
                        help="run both arms against the API, about ten cents")
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
        live(arguments.items) if arguments.live else replay(arguments.items)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
