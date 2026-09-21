"""
Finding the cut, and refusing to find one that is not there.

`calibrate` turns a judge's own certainty into a threshold, which is the sort of
thing that produces a number whether or not there is anything behind it. So the
test that matters most here is the negative one: given certainty that says
nothing, it has to come back saying nothing, not with a confident cut fitted to
noise.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev_ultralightspeed import Answer, Calibration, JevError, calibrate  # noqa: E402


def a_judge(count: int, *, honest: bool, seed: int = 1, kind: str = "choice"):
    """
    Answers and their labels, with certainty either meaning something or not.

    Honest: the surer it is the more often it is right, which is the whole
    premise. Otherwise certainty is random noise stapled to the same accuracy,
    which is what a judge whose probability carries no information looks like.
    """
    dice = random.Random(seed)
    answers, gold = [], []
    for _ in range(count):
        certainty = dice.uniform(0.5, 1.0)
        chance = certainty if honest else 0.85
        right = dice.random() < chance
        answers.append(Answer(item="", p=certainty, label="a" if right else "b", kind=kind))
        gold.append("a")
    return answers, gold


def test_it_finds_a_cut_that_is_really_there():
    answers, gold = a_judge(2_000, honest=True)
    cal = Calibration.from_answers(answers, gold, keep=0.8)
    assert 0.75 <= cal.coverage <= 0.85, "keep=0.8 should keep about four fifths"
    assert cal.gain > 0.03, f"the cut bought only {cal.gain:.1%}, and it should buy more"
    assert cal.low <= cal.accuracy <= cal.high


def test_it_does_not_find_a_cut_that_is_not_there():
    """
    The one that keeps this honest.

    Certainty is noise here, so no threshold can do better than not cutting at
    all, and on rows the cut has not seen it must not appear to.
    """
    answers, gold = a_judge(2_000, honest=False)
    cal = Calibration.from_answers(answers, gold, keep=0.8)
    assert abs(cal.gain) < 0.03, \
        f"found {cal.gain:+.1%} of agreement in pure noise, which is not there"
    assert cal.low < cal.baseline < cal.high, "and the baseline should sit inside the spread"


def test_asking_for_a_bar_gives_back_the_coverage_it_costs():
    answers, gold = a_judge(2_000, honest=True)
    loose = Calibration.from_answers(answers, gold, accuracy=0.90)
    tight = Calibration.from_answers(answers, gold, accuracy=0.97)
    assert tight.cut > loose.cut, "a higher bar needs a higher cut"
    assert tight.coverage < loose.coverage, "and keeps less"


def test_a_bar_nothing_reaches_is_an_error_and_says_what_was_possible():
    answers, gold = a_judge(500, honest=False)     # 85% and no ranking to exploit
    with pytest.raises(JevError) as raised:
        Calibration.from_answers(answers, gold, accuracy=0.999)
    assert "best" in str(raised.value) or "manages" in str(raised.value)


def test_it_says_when_the_held_out_rows_fell_short_of_the_bar():
    """
    A cut fitted on every row flatters itself. Handing back a number under the
    one that was asked for without saying so would waste the held-out halves.
    """
    answers, gold = a_judge(1_000, honest=True)
    cal = Calibration.from_answers(answers, gold, accuracy=0.97)
    if cal.accuracy < 0.97:
        assert any("asked for" in note for note in cal.notes)


def test_a_judge_sure_of_everything_says_so():
    """Seen on AG News, where four judgements in five come back at 1.000."""
    answers = [Answer(item="", p=1.0, label="a", kind="choice") for _ in range(900)]
    answers += [Answer(item="", p=0.6, label="b", kind="choice") for _ in range(100)]
    gold = ["a"] * 1_000
    cal = Calibration.from_answers(answers, gold, keep=0.5)
    assert any("tie at" in note for note in cal.notes)


def test_a_yes_no_is_ranked_by_distance_from_the_coin_flip():
    """
    `p` on a noul is the probability of yes, so a confident no has a low one.
    Ranking on `p` would put the firmest verdicts at the bottom; `certainty` is
    what makes a confident no and a confident yes sit together at the top.
    """
    answers = [Answer(item="", p=0.01, label="no", kind="noul") for _ in range(100)]
    answers += [Answer(item="", p=0.55, label="yes", kind="noul") for _ in range(100)]
    gold = ["no"] * 100 + ["no"] * 100        # the sure ones right, the unsure ones wrong
    cal = Calibration.from_answers(answers, gold, keep=0.5)
    assert cal.accuracy > 0.9, "the confident no's should have been the half it kept"


def test_skipped_answers_are_left_out_and_mentioned():
    answers, gold = a_judge(300, honest=True)
    answers[0] = Answer(item="", p=0.0, label="", kind="error", error="nope")
    cal = Calibration.from_answers(answers, gold, keep=0.8)
    assert cal.items == 299
    assert any("unusable" in note for note in cal.notes)


def test_thin_evidence_is_called_thin():
    answers, gold = a_judge(60, honest=True)
    assert any("thin" in note for note in Calibration.from_answers(answers, gold, keep=0.8).notes)


def test_too_few_rows_to_say_anything_is_refused():
    answers, gold = a_judge(10, honest=True)
    with pytest.raises(JevError, match="too few"):
        Calibration.from_answers(answers, gold, keep=0.8)


@pytest.mark.parametrize("targets", [{}, {"keep": 0.8, "accuracy": 0.9}])
def test_one_target_or_the_other_and_not_both(targets):
    answers, gold = a_judge(300, honest=True)
    with pytest.raises(JevError, match="one of keep or accuracy"):
        Calibration.from_answers(answers, gold, **targets)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_a_target_outside_the_range_is_refused(bad):
    answers, gold = a_judge(300, honest=True)
    with pytest.raises(JevError, match="fraction"):
        Calibration.from_answers(answers, gold, keep=bad)


def test_labels_that_do_not_line_up_are_refused():
    answers, gold = a_judge(300, honest=True)
    with pytest.raises(JevError, match="line up"):
        Calibration.from_answers(answers, gold[:299], keep=0.8)


def test_calibrate_asks_once_and_leaves_a_borrowed_client_open():
    """The live entry point, with the one method that talks to the API replaced."""
    answers, gold = a_judge(400, honest=True)
    items = [f"row {index}" for index in range(400)]

    class Borrowed:
        def __init__(self):
            self.asked, self.closed = 0, False

        def classify(self, rows, instructions, **_):
            self.asked += 1
            assert len(rows) == len(items) and instructions == "Is it?"
            return answers

        def close(self):
            self.closed = True

    client = Borrowed()
    cal = calibrate(items, gold, "Is it?", keep=0.8, client=client)
    assert client.asked == 1
    assert not client.closed, "a client that was passed in is not ours to close"
    assert cal.items == 400


def test_calibrate_checks_the_lengths_before_spending_anything():
    class Explodes:
        def classify(self, *_args, **_kwargs):
            raise AssertionError("should never have been asked")

    with pytest.raises(JevError, match="line up"):
        calibrate(["a", "b"], ["yes"], "Is it?", keep=0.8, client=Explodes())
