"""
The arithmetic behind the published numbers.

The benches are not the library, but their output is what gets written down, and
this repository has withdrawn two throughput claims already: one that counted a
retry-dragged run as a rate, one that let two arms share a limiter. Both were
arithmetic, and neither was caught by a test because none of this had any.

What is checked here is the part where a wrong answer looks plausible: turning a
series of counter readings into a rate. Everything else in a bench either prints
or costs money.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

import sustained as bench_sustained  # noqa: E402


def test_every_script_the_readme_tells_you_to_run_is_there():
    """
    The README is the instructions, and a moved file turns them into a typo
    nobody notices until a stranger runs one. Cheap to check, so it is checked
    rather than remembered: the benches moved into bench/ once and there were
    thirty-nine references to update.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    named = sorted(set(re.findall(r"[A-Za-z0-9_./-]+\.py",
                                  (root / "README.md").read_text(encoding="utf-8"))))
    assert named, "no scripts named in the README at all, which is not right either"
    missing = [name for name in named if not (root / name).exists()]
    assert not missing, f"the README points at {missing}, which is not there"


def readings(pairs, tokens_each=100):
    """Counter samples as the watcher thread would have taken them."""
    return [{"t": float(t), "requests": n, "items": n * 32,
             "input_tokens": n * tokens_each, "retries": 0, "pushback": 0, "waited": 0.0}
            for t, n in pairs]


def test_a_steady_rate_reads_back_as_that_rate():
    # 1,000 requests a minute, dead flat, sampled every ten seconds for three minutes.
    samples = readings([(t, int(t * 1000 / 60)) for t in range(0, 190, 10)])
    rows = bench_sustained.windows(samples, 60.0, pack=32)
    assert len(rows) == 3, "three full minutes should give three windows"
    for row in rows:
        assert 950 < row["requests_per_minute"] < 1050
        # Items come from requests x pack, not the items counter, which moves a
        # whole chunk at a time and would report a staircase.
        assert row["items_per_second"] == row["requests_per_minute"] * 32 / 60


def test_a_run_shorter_than_a_window_reports_nothing():
    """
    The failure this bench exists to prevent.

    A sub-minute run cannot tell a burst from a rate, so it must not produce a
    row that looks like one.
    """
    samples = readings([(0, 0), (30, 900)])
    assert bench_sustained.windows(samples, 60.0, pack=32) == []


def test_a_breach_that_straddles_two_minutes_is_still_a_breach():
    """
    Why the ceiling check slides instead of counting whole minutes.

    900 permits in the back half of one minute and 900 in the front half of the
    next is 900 a minute by the clock and 1,800 in the sixty seconds the server
    actually saw. Anchored windows would call this obedient.
    """
    admitted = [30.0 + i * 0.03 for i in range(900)] + [60.0 + i * 0.03 for i in range(900)]
    worst, began = bench_sustained.worst_window(admitted, 60.0)
    assert worst == 1800
    assert began == 0.0


def test_the_worst_window_says_when_it_started():
    # A quiet first minute, then 1,000 permits inside one. The gap is wider than
    # the window, so no window can hold a piece of both and the answer is exact.
    admitted = [i * 0.5 for i in range(100)] + [120.0 + i * 0.05 for i in range(1000)]
    worst, began = bench_sustained.worst_window(admitted, 60.0)
    assert worst == 1000
    assert began == 120.0


def test_the_ceiling_check_reads_permits_and_not_completions():
    """
    The mistake this bench made on its first real run.

    `usage.requests` counts responses coming back. Responses bunch, so the
    completion counter can show more in a window than the limiter ever let out,
    and calling that a breach is reading the wrong event. These are the permits,
    exactly on the ceiling, and nothing here should look like a breach.
    """
    admitted = [i * 0.06 for i in range(1000)]          # 1,000 evenly across a minute
    worst, _began = bench_sustained.worst_window(admitted, 60.0)
    assert worst <= 1000


def test_no_permits_is_not_a_breach():
    assert bench_sustained.worst_window([], 60.0) == (0, 0.0)


# -- the ceiling itself ------------------------------------------------------

def test_the_ceiling_holds_in_every_window_there_is(monkeypatch):
    """
    The claim the sustained bench cannot make and this can.

    A live run stamps each permit just after it is granted, and that jitter is
    worth about one request at a window edge, so a live run can read one over and
    mean nothing by it. Here the clock is ours and the stamp is the limiter's own,
    so the count is exact: over an hour of simulated traffic offered faster than
    the ceiling, no sixty seconds may ever hold more than the ceiling allows.
    """
    from jev_ultralightspeed import _limits

    CEILING, OFFERED = 1_000, 0.02        # a request offered every 20ms, 3,000 a minute
    now = [0.0]
    monkeypatch.setattr(_limits.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(_limits.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    limiter = _limits._Limiter(CEILING)
    granted = []
    while now[0] < 3_600.0:
        limiter.take()                    # blocks by moving our clock, never by waiting
        granted.append(limiter._recent[-1])
        now[0] += OFFERED

    assert len(granted) > 50_000, "the offered load has to exceed the ceiling to test it"
    start, worst = 0, 0
    for end, moment in enumerate(granted):
        while moment - granted[start] >= 60.0:      # the limiter evicts at exactly 60
            start += 1
        worst = max(worst, end - start + 1)
    assert worst <= CEILING, f"{worst} permits inside one window, ceiling is {CEILING}"


def test_a_stamp_exactly_sixty_seconds_old_is_out_of_the_window(monkeypatch):
    """
    The boundary the whole count turns on, pinned so it cannot drift.

    At exactly sixty seconds the oldest permit has left the window and the next
    one goes immediately. A hair under and it has not.
    """
    from jev_ultralightspeed import _limits

    now = [0.0]
    monkeypatch.setattr(_limits.time, "monotonic", lambda: now[0])
    limiter = _limits._Limiter(2)
    assert limiter.try_take() == 0.0
    assert limiter.try_take() == 0.0
    assert limiter.try_take() > 0.0, "the third inside the window has to wait"

    now[0] = 59.999
    assert limiter.try_take() > 0.0, "a hair under sixty seconds is still inside"
    now[0] = 60.0
    assert limiter.try_take() == 0.0, "at sixty seconds the oldest is gone"


def test_an_older_file_still_reads(tmp_path):
    """The first runs stored pushback per status code. They should not stop opening."""
    path = tmp_path / "old.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "jev-sustained-run", "pack": 32, "rpm": 1000}) + "\n")
        for t, n in [(0, 0), (60, 900)]:
            handle.write(json.dumps({"t": float(t), "requests": n, "items": n * 32,
                                     "input_tokens": n * 100, "retries": 3,
                                     "pushback": {"429": 2, "503": 1}, "waited": 1.5}) + "\n")
    _head, samples = bench_sustained.read(path)
    assert [sample["pushback"] for sample in samples] == [3, 3]
    assert bench_sustained.windows(samples, 60.0, pack=32)[0]["pushback"] == 0
