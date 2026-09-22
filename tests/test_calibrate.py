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

from jev_ultralightspeed import (  # noqa: E402
    Answer,
    Calibration,
    JevError,
    calibrate,
    discrimination,
    resolution,
    routing,
)


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


def test_discrimination_knows_perfect_separation_from_none():
    """The two ends of the scale, so the middle means something."""
    perfect = ([Answer(item="", p=0.9, label="a", kind="choice") for _ in range(50)]
               + [Answer(item="", p=0.1, label="b", kind="choice") for _ in range(50)])
    assert discrimination(perfect, ["a"] * 100) == 1.0

    # Every answer carries the same certainty, so it cannot rank anything. Ties
    # share an averaged rank, which is what keeps this at a coin flip instead of
    # rewarding whatever order the answers arrived in.
    flat = [Answer(item="", p=0.7, label="a" if i % 2 else "b", kind="choice")
            for i in range(100)]
    assert discrimination(flat, ["a"] * 100) == 0.5


def test_discrimination_matches_a_hand_worked_case():
    """Three right and one wrong, with the wrong one second from the top."""
    answers = [Answer(item="", p=p, label=label, kind="choice")
               for p, label in ((0.9, "a"), (0.8, "b"), (0.7, "a"), (0.6, "a"))]
    # Sorted by certainty the labels run a, a, b, a, so of the 3 x 1 right/wrong
    # pairs the wrong one sits above two of the right ones. Only 1 of 3 pairs is
    # ordered the way it should be.
    assert discrimination(answers, ["a"] * 4) == pytest.approx(1 / 3)


def test_calibrate_refuses_when_the_certainty_is_noise():
    """
    The thing this is for.

    A judge whose certainty cannot tell its right answers from its wrong ones
    has nothing for a threshold to sort by, so the honest reply is no, not a
    number. Asked for one anyway, the old behaviour handed back a small positive
    gain that was the sample flattering itself.
    """
    answers, gold = a_judge(2_000, honest=False)
    assert discrimination(answers, gold) < 0.6
    with pytest.raises(JevError, match="coin flip"):
        Calibration.from_answers(answers, gold, keep=0.8)
    with pytest.raises(JevError, match="coin flip"):
        Calibration.from_answers(answers, gold, accuracy=0.9)


def test_the_refusal_says_what_it_measured_and_how_to_override():
    answers, gold = a_judge(2_000, honest=False)
    with pytest.raises(JevError) as raised:
        Calibration.from_answers(answers, gold, keep=0.8)
    said = str(raised.value)
    assert "0.5" in said, "it should name the coin flip it is comparing against"
    assert "min_signal" in said, "and the way to get the number regardless"

    # Lowered deliberately, it answers, because the caller has said they know.
    cal = Calibration.from_answers(answers, gold, keep=0.8, min_signal=0.0)
    assert abs(cal.gain) < 0.03


def test_a_real_signal_is_not_refused_and_is_reported():
    answers, gold = a_judge(2_000, honest=True)
    cal = Calibration.from_answers(answers, gold, keep=0.8)
    assert cal.discrimination > 0.6
    assert f"{cal.discrimination:.3f}" in str(cal), "printing it should show the number"


def test_a_bound_lands_above_the_bar_where_an_estimate_lands_on_it():
    """
    The difference between "these rows scored 95% here" and "these rows will
    score 95%".

    Without `confidence` the cut is the longest prefix whose observed rate hits
    the target, which overshoots by however much the sample flattered it, so the
    held-out figure sits on the bar and deployment is a coin flip either side of
    it. With a bound it sits above.
    """
    answers, gold = a_judge(4_000, honest=True)
    estimate = Calibration.from_answers(answers, gold, accuracy=0.95)
    bounded = Calibration.from_answers(answers, gold, accuracy=0.95, confidence=0.95)

    assert bounded.cut > estimate.cut, "a bound has to cut higher than an estimate"
    assert bounded.coverage < estimate.coverage, "and therefore keep less"
    assert bounded.accuracy > estimate.accuracy, "and land further above the bar"


def test_more_confidence_cuts_higher():
    answers, gold = a_judge(4_000, honest=True)
    cuts = [Calibration.from_answers(answers, gold, accuracy=0.95, confidence=c).cut
            for c in (0.80, 0.90, 0.95, 0.99)]
    assert cuts == sorted(cuts), f"cuts should rise with confidence, got {cuts}"


def test_headroom_says_whether_the_margin_is_what_is_holding_coverage_back():
    """
    Wide headroom means the bound is the constraint and more labels would move
    it. Nothing to compare against without a bound, so it stays at zero.
    """
    answers, gold = a_judge(4_000, honest=True)
    assert Calibration.from_answers(answers, gold, accuracy=0.95).headroom == 0.0
    bounded = Calibration.from_answers(answers, gold, accuracy=0.95, confidence=0.99)
    assert bounded.headroom > 0.0


def test_too_few_rows_to_certify_says_so_on_its_own_terms():
    """
    The message has to be quoted on the footing the search used. Reporting the
    observed rate while refusing on the bound reads as a contradiction: nothing
    is certifiable, and yet the best manages 100%.
    """
    answers, gold = a_judge(400, honest=True)
    with pytest.raises(JevError) as raised:
        Calibration.from_answers(answers, gold, accuracy=0.95, confidence=0.99)
    said = str(raised.value)
    assert "guarantee" in said
    assert "400 rows" in said, "it should name the count, which is usually the constraint"


@pytest.mark.parametrize("bad", [0.77, 0.5001, 1.0])
def test_an_unsupported_confidence_is_refused_with_the_list(bad):
    answers, gold = a_judge(600, honest=True)
    with pytest.raises(JevError, match="confidence is one of"):
        Calibration.from_answers(answers, gold, accuracy=0.95, confidence=bad)


def test_confidence_with_keep_is_refused_because_there_is_no_bar():
    answers, gold = a_judge(600, honest=True)
    with pytest.raises(JevError, match="applies to accuracy"):
        Calibration.from_answers(answers, gold, keep=0.8, confidence=0.95)


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
    # The gate now refuses this outright, which its own test covers. Asked past
    # it deliberately, the arithmetic underneath still has to find nothing.
    cal = Calibration.from_answers(answers, gold, keep=0.8, min_signal=0.0)
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
    # A judge whose ranking is real but whose best is 90%: the signal gate lets
    # it through, and then no cut can reach the bar, which is the path under
    # test. `a_judge(honest=True)` will not do, because there the surest answers
    # are right almost always and a small enough slice of them hits any target.
    dice = random.Random(3)
    answers, gold = [], []
    for _ in range(500):
        certainty = dice.uniform(0.5, 1.0)
        right = dice.random() < 0.5 + 0.8 * (certainty - 0.5)     # 50% up to 90%
        answers.append(Answer(item="", p=certainty, label="a" if right else "b", kind="choice"))
        gold.append("a")

    assert discrimination(answers, gold) > 0.6, "this fixture has to clear the gate"
    with pytest.raises(JevError) as raised:
        Calibration.from_answers(answers, gold, accuracy=0.999)
    assert "manages" in str(raised.value), "it should say what the best any cut managed"


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


# -- a cut has to deliver the bar it was asked for ---------------------------

def a_crowded_judge(block: int, tail: int, block_accuracy: float, *, seed: int = 3):
    """
    A judge that reports one certainty for most of its work.

    Which is what real ones do: measured on a provider, 65% of a corpus came
    back at exactly 1.000. A cut keeps that whole block or none of it, so a bar
    the block cannot meet on its own is unreachable at any coverage, however
    many labels arrive.
    """
    dice = random.Random(seed)
    answers, gold = [], []
    for index in range(block):
        right = index < round(block * block_accuracy)
        answers.append(Answer(item="", p=1.0, label="a", kind="choice"))
        gold.append("a" if right else "b")
    for _ in range(tail):
        sure = dice.uniform(0.5, 0.95)
        answers.append(Answer(item="", p=round(sure, 2), label="a", kind="choice"))
        gold.append("a" if dice.random() < sure else "b")
    order = list(range(len(answers)))
    dice.shuffle(order)
    return [answers[i] for i in order], [gold[i] for i in order]


@pytest.mark.parametrize("bar", [0.90, 0.95, 0.97, 0.99])
@pytest.mark.parametrize("confidence", [0.0, 0.90])
def test_a_returned_cut_always_delivers_the_bar_it_promised(bar, confidence):
    """
    The regression that matters. A cut used to be scored on a prefix that
    stopped inside a block of equal certainties, then applied as
    `certainty >= cut`, which keeps the whole block. On real output that
    returned a cut keeping 65% of the rows at 96.5% for a 97% bar, with the
    bound engaged, because the bound had been computed over part of the block.
    """
    answers, gold = a_crowded_judge(2_000, 1_000, 0.965)
    try:
        cal = Calibration.from_answers(answers, gold, accuracy=bar,
                                       confidence=confidence)
    except JevError:
        return                      # refusing is the other correct answer
    kept = [1.0 if answer.label == want else 0.0
            for answer, want in zip(answers, gold, strict=True)
            if answer.certainty >= cal.cut]
    assert kept, "a cut that keeps nothing should have been a refusal"
    assert sum(kept) / len(kept) >= bar, (
        f"asked for {bar:.0%}, the cut {cal.cut:.3f} delivers "
        f"{sum(kept) / len(kept):.2%} over {len(kept)} kept")


def test_an_unreachable_bar_blames_the_block_and_not_the_label_count():
    """
    The advice has to match the cause. Telling someone to label more rows when
    their judge has crowded two thirds of its answers onto one value sends them
    off to buy labels that cannot help.
    """
    answers, gold = a_crowded_judge(2_000, 1_000, 0.965)
    with pytest.raises(JevError) as raised:
        Calibration.from_answers(answers, gold, accuracy=0.99, confidence=0.90)
    said = str(raised.value)
    assert "more labels will not change that" in said
    assert "tied at" in said
    assert "several times" in said, "the remedy is more asks per item"
    assert "label more rows" not in said


def test_resolution_reports_the_block_and_whether_the_bar_is_reachable():
    answers, gold = a_crowded_judge(2_000, 1_000, 0.965)
    grain = resolution(answers, gold, accuracy=0.99)
    assert grain.crowd == pytest.approx(1.0)
    assert grain.share == pytest.approx(2 / 3, abs=0.01)
    assert grain.crowd_accuracy == pytest.approx(0.965, abs=0.005)
    assert grain.blocked, "0.99 is above what the block manages on its own"

    easy = resolution(answers, gold, accuracy=0.90)
    assert not easy.blocked
    assert easy.reachable > 0.6, "the block alone clears 90%"

    assert resolution(answers, gold).reachable is None, "no bar was asked about"


def test_resolution_is_not_alarmed_by_a_judge_that_spreads_its_certainty():
    answers, gold = a_judge(1_000, honest=True)
    grain = resolution(answers, gold, accuracy=0.90)
    assert grain.levels > 20, "an honest spread should use many values"
    assert grain.share < 0.20


def test_a_fine_grained_judge_reaches_bars_a_crowded_one_cannot():
    """
    Same accuracy, different resolution. The only thing that changes is how many
    distinct certainties came back, and it decides whether a cut exists.
    """
    crowded, crowded_gold = a_crowded_judge(2_000, 1_000, 0.965)
    assert resolution(crowded, crowded_gold, accuracy=0.99).blocked

    spread, spread_gold = a_judge(3_000, honest=True, seed=5)
    grain = resolution(spread, spread_gold, accuracy=0.99)
    assert grain.levels > grain.share * 100


# -- one verdict per label, because one cut over all of them is an average ---

def a_judge_with_unlike_labels(seed: int = 11):
    """
    Three labels, three different relationships between certainty and truth.

    Modelled on a real measurement: one label where the judge is right almost
    always and its certainty orders the rest correctly, one where certainty runs
    backwards, and one where it is wrong nearly every time. Pooled they look
    like a mediocre judge with no signal, which is exactly the point.
    """
    dice = random.Random(seed)
    answers, gold = [], []

    for _ in range(300):                      # trustworthy, and well ordered
        sure = dice.uniform(0.5, 1.0)
        answers.append(Answer(item="", p=round(sure, 3), label="good", kind="choice"))
        gold.append("good" if dice.random() < 0.90 + 0.09 * sure else "bad")
    for _ in range(600):                      # certainty pointing the wrong way
        sure = dice.uniform(0.5, 1.0)
        answers.append(Answer(item="", p=round(sure, 3), label="backwards", kind="choice"))
        gold.append("backwards" if dice.random() < 0.95 - 0.6 * sure else "good")
    for _ in range(300):                      # almost always wrong, unsortable
        answers.append(Answer(item="", p=round(dice.uniform(0.5, 1.0), 3),
                              label="hopeless", kind="choice"))
        gold.append("hopeless" if dice.random() < 0.07 else "good")

    order = list(range(len(answers)))
    dice.shuffle(order)
    return [answers[i] for i in order], [gold[i] for i in order]


def test_routing_tells_the_three_kinds_of_label_apart():
    answers, gold = a_judge_with_unlike_labels()
    verdicts = {v.label: v for v in routing(answers, gold, accuracy=0.90)}
    assert set(verdicts) == {"good", "backwards", "hopeless"}

    assert verdicts["good"].action in ("take", "cut")
    assert verdicts["good"].discrimination > 0.5

    assert verdicts["backwards"].action == "inverted", verdicts["backwards"]
    assert verdicts["backwards"].high < 0.5, (
        "an inverted verdict needs its interval below a coin flip")
    assert verdicts["backwards"].cut is None or verdicts["backwards"].coverage == 0.0

    assert verdicts["hopeless"].action == "send"
    assert verdicts["hopeless"].accuracy < 0.2


def test_routing_finds_a_class_worth_keeping_inside_a_judge_worth_refusing():
    """The whole reason this exists: the pooled number throws the good class away."""
    answers, gold = a_judge_with_unlike_labels()
    pooled = discrimination(answers, gold)
    assert pooled < 0.65, "the pooled figure should look unpromising here"

    best = max(routing(answers, gold, accuracy=0.90),
               key=lambda v: v.discrimination if v.discrimination == v.discrimination else 0)
    assert best.discrimination > pooled + 0.15, (
        f"pooled {pooled:.3f} should hide a much better class, best was "
        f"{best.discrimination:.3f}")


def test_a_label_with_too_few_answers_is_not_judged():
    answers, gold = a_judge_with_unlike_labels()
    answers += [Answer(item="", p=0.9, label="rare", kind="choice") for _ in range(12)]
    gold += ["rare"] * 12
    verdicts = {v.label: v for v in routing(answers, gold, accuracy=0.90)}
    assert verdicts["rare"].action == "unknown"
    assert "too few" in verdicts["rare"].why


def test_calibrate_says_so_when_its_labels_disagree():
    """
    A caller who never reaches for routing() still has to be told.

    The bar has to be one this judge can actually reach, or calibrate refuses
    before it gets as far as writing any notes, which is its own correct
    behaviour and not what this test is about.
    """
    answers, gold = a_judge_with_unlike_labels()
    cal = Calibration.from_answers(answers, gold, accuracy=0.60, min_signal=0.0)
    said = " ".join(cal.notes)
    assert "routing()" in said, cal
    assert "backwards" in said or "better inside some" in said, cal


def test_routing_refuses_a_bar_that_is_not_a_fraction():
    answers, gold = a_judge_with_unlike_labels()
    for bad in (0.0, 1.0, 1.4, -0.2):
        with pytest.raises(JevError):
            routing(answers, gold, accuracy=bad)


def test_routing_and_calibrate_agree_about_confidence_levels():
    answers, gold = a_judge_with_unlike_labels()
    with pytest.raises(JevError):
        routing(answers, gold, accuracy=0.90, confidence=0.77)
    assert routing(answers, gold, accuracy=0.90, confidence=0.95)


def test_a_bound_only_ever_lowers_a_class_verdict():
    """The floor is a lower bound, so asking for confidence cannot flatter a class."""
    answers, gold = a_judge_with_unlike_labels()
    loose = {v.label: v for v in routing(answers, gold, accuracy=0.90)}
    tight = {v.label: v for v in routing(answers, gold, accuracy=0.90, confidence=0.95)}
    for label in loose:
        assert tight[label].floor <= loose[label].floor + 1e-9, label
