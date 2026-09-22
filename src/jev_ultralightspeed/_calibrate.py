"""
Finding the cut, on your own labelled rows.

`triage` needs a number and this library cannot tell you what it is. Measured on
three corpora the cut that keeps four fifths was 0.92, 0.77 and 0.95, and the
direction of the miscalibration flipped between them, so there is nothing here to
carry across. What does carry across is the method, and that is what this is: an
hour of somebody's afternoon turned into one call over a few hundred rows you
already have labels for.

    cal = calibrate(rows, labels, "Does this need a human?", accuracy=0.95)
    print(cal)
    trusted, review = triage(answers, at_least=cal.cut)

Two things it does that doing it by hand usually does not.

The cut is chosen on all of your rows, because that is the one you are going to
deploy and it should see everything. What to *expect* from it is measured on rows
it was not chosen on, over many random splits, because a threshold scored against
the data that picked it is not a measurement. Those are different questions and
this keeps them apart.

And it says when not to believe it. Too few rows, a judge so sure of everything
that the ranking has no resolution left, a target nothing reaches: all of those
come back as notes rather than as a confident number.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ._answers import Answer
from ._errors import JevError

# Below this many labelled rows the held-out halves are too small to say much,
# and the interval will show it, but a note is more honest than a wide bar.
FEW = 200
# The smallest slice a cut may be fitted to. A prefix of five items is 100% right
# often enough to fool a search that is looking for 95%.
FLOOR = 30
SPLITS = 20
SEED = 7
# Below this, the ranking a threshold would sort by is not telling right answers
# from wrong ones, so there is nothing for a cut to find. 0.5 is a coin flip;
# 0.6 leaves room for a weak but real signal and still catches noise. Lower it
# deliberately with `min_signal` if you know your ranking is faint and you want
# the number anyway.
MIN_SIGNAL = 0.60
# z for a one-sided bound at a few ordinary confidence levels. Anything else
# is interpolated badly, so these are the ones offered.
Z_FOR = {0.50: 0.0, 0.80: 0.84, 0.90: 1.28, 0.95: 1.645, 0.99: 2.33}


@dataclass(frozen=True)
class Calibration:
    """
    A cut, and how much to believe it.

    `cut` is the number to hand to `triage(at_least=...)`. `coverage` and
    `accuracy` are what it did on rows it was not chosen on; `low` and `high` are
    the middle 90% of that across the splits, so they are the honest width of the
    estimate. `baseline` is the agreement with no cut at all, which is what the
    cut has to beat to be worth anything.
    """

    cut: float
    coverage: float
    accuracy: float
    low: float
    high: float
    baseline: float
    items: int
    splits: int
    discrimination: float = 0.0
    headroom: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def gain(self) -> float:
        """Points of agreement the cut buys over not cutting."""
        return self.accuracy - self.baseline

    def __str__(self) -> str:
        lines = [
            f"cut {self.cut:.3f}, keeping {self.coverage:.0%} at {self.accuracy:.1%} agreement",
            f"  without a cut           {self.baseline:>6.1%}",
            f"  with it                 {self.accuracy:>6.1%}  ({self.gain * 100:+.1f} points)",
            f"  over {self.splits} held-out splits  {self.low:.1%} to {self.high:.1%}",
            f"  certainty separates right from wrong at {self.discrimination:.3f} "
            f"(0.5 is a coin flip)",
            f"  measured on {self.items:,} labelled rows",
        ]
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)

    @classmethod
    def from_answers(cls, answers: Sequence[Answer], gold: Sequence[str], *,
                     keep: float | None = None, accuracy: float | None = None,
                     splits: int = SPLITS, seed: int = SEED,
                     min_signal: float = MIN_SIGNAL,
                     confidence: float = 0.0) -> Calibration:
        """
        The same thing on answers you already have, which costs nothing.

        Use this to re-cut a finished run at a different target, or to calibrate
        without asking twice.
        """
        return _work(list(answers), list(gold), keep, accuracy, splits, seed, min_signal,
                     confidence)


def calibrate(items: Sequence[str], gold: Sequence[str], instructions: str, *,
              keep: float | None = None, accuracy: float | None = None,
              criteria: dict | None = None, options: dict | None = None,
              levels: Sequence[str] | None = None,
              client=None, splits: int = SPLITS, seed: int = SEED,
              min_signal: float = MIN_SIGNAL,
              confidence: float = 0.0) -> Calibration:
    """
    Ask the judge about labelled rows, then find the cut worth trusting.

        cal = calibrate(rows, labels, "Is this urgent?", accuracy=0.95)
        cal = calibrate(rows, labels, "Which team?", options=teams, keep=0.8)

    `gold` is what each row should have come back as, in the same order: "yes" or
    "no" for a yes/no question, the option key for a pick-one, the level for a
    score. Say one of `keep` (how much you want to automate) or `accuracy` (how
    right it has to be), not both.

    A few hundred rows is enough and the call costs cents. Pass a `client` to
    control packing or the key; otherwise a default one is made and closed here.

    What comes back is a `Calibration`. Print it before using it: it carries the
    width of its own estimate and the reasons not to trust it, if there are any.
    """
    if len(items) != len(gold):
        raise JevError(f"{len(items)} items and {len(gold)} labels; they have to line up")

    from ._client import Client

    own = client is None
    client = client or Client()
    try:
        answers = client.classify(list(items), instructions, criteria=criteria,
                                  options=options, levels=levels)
    finally:
        if own:
            client.close()
    return _work(answers, list(gold), keep, accuracy, splits, seed, min_signal, confidence)


# -- the arithmetic ----------------------------------------------------------

def discrimination(answers: Sequence[Answer], gold: Sequence[str]) -> float:
    """
    Can this judge's certainty tell its right answers from its wrong ones?

        >>> discrimination(answers, labels)
        0.87

    The area under the ROC curve over (certainty, was it right). 1.0 is perfect
    separation, 0.5 is a coin flip, and 0.5 is what a judge out of its depth
    gives you.

    This is the question to ask before asking where to cut, because a threshold
    sorts by certainty and keeps the top of the pile. If the certainty does not
    know which answers are wrong, sorting by it is sorting by noise and any gain
    that comes back is the sample flattering itself.

    It is also not visible from the answers. A judge can report ordinary-looking
    certainties, well spread, none of them extreme, and still be at 0.5. Only
    labels reveal it, which is why this takes them.

    Ties share an averaged rank, so a judge that reports the same number for
    every item scores 0.5 rather than whatever order it happened to arrive in.
    """
    rows = _scored(answers, gold)
    if not rows:
        raise JevError("no usable answers to measure")
    area = _auroc(rows)
    if area != area:
        raise JevError("every answer went the same way, so there is nothing to separate")
    return area


def _auroc(rows: list[tuple[float, float]]) -> float:
    """The Mann-Whitney statistic, or nan when every answer went one way."""
    pairs = sorted(rows, key=lambda row: row[0])
    ranks: dict[int, float] = {}
    index = 0
    while index < len(pairs):
        last = index
        while last + 1 < len(pairs) and pairs[last + 1][0] == pairs[index][0]:
            last += 1
        for position in range(index, last + 1):
            ranks[position] = (index + last) / 2 + 1
        index = last + 1
    right = sum(correct for _certainty, correct in pairs)
    wrong = len(pairs) - right
    if not right or not wrong:
        return float("nan")
    got = sum(ranks[position] for position, (_c, correct) in enumerate(pairs) if correct)
    return (got - right * (right + 1) / 2) / (right * wrong)


def _auroc_interval(rows: list[tuple[float, float]]) -> tuple[float, float, float]:
    """
    AUROC and a 95% interval, closed form.

    Hanley and McNeil 1982, checked against a 1,000 round bootstrap on real
    runs: at n=944 the two agree to three decimals, and at n=175 this one is
    wider, [0.642, 1.000] against [0.690, 0.977]. Erring wide at small n is the
    right direction for a check that decides whether to trust a class, and it
    avoids a bootstrap nobody will wait for.
    """
    area = _auroc(rows)
    right = sum(correct for _certainty, correct in rows)
    wrong = len(rows) - right
    if area != area or not right or not wrong:
        return area, float("nan"), float("nan")
    q1 = area / (2 - area)
    q2 = 2 * area * area / (1 + area)
    variance = (area * (1 - area) + (right - 1) * (q1 - area * area)
                + (wrong - 1) * (q2 - area * area)) / (right * wrong)
    spread = 1.96 * math.sqrt(max(variance, 0.0))
    return area, max(0.0, area - spread), min(1.0, area + spread)


def _z_for(confidence: float) -> float:
    """The one-sided z for a confidence level, or zero for a point estimate."""
    if confidence and confidence not in Z_FOR:
        raise JevError(f"confidence is one of {sorted(Z_FOR)}, not {confidence}")
    return Z_FOR.get(confidence or 0.0, 0.0)


def _scored(answers: Sequence[Answer], gold: Sequence[str]) -> list[tuple[float, float]]:
    """(certainty, 1 if right else 0) for every answer that came back at all."""
    if len(answers) != len(gold):
        raise JevError(f"{len(answers)} answers and {len(gold)} labels; they have to line up")
    return [(answer.certainty, 1.0 if answer.label == want else 0.0)
            for answer, want in zip(answers, gold, strict=True) if answer.ok]


def _cut_for_keep(rows: list[tuple[float, float]], keep: float) -> float:
    """The certainty at which `keep` of these rows sits at or above it."""
    ranked = sorted((certainty for certainty, _ in rows), reverse=True)
    return ranked[min(len(ranked) - 1, max(0, int(len(ranked) * keep) - 1))]


def _wilson_lower(hat: float, total: int, z: float) -> float:
    """
    The low end of a confidence interval on a proportion.

    Wilson rather than the textbook normal interval, because at a few hundred
    rows and at proportions near one, which is exactly where a threshold lives,
    the normal interval runs off the end and claims coverage it has not got.
    """
    if total <= 0:
        return 0.0
    denominator = 1 + z * z / total
    centre = hat + z * z / (2 * total)
    spread = z * math.sqrt(hat * (1 - hat) / total + z * z / (4 * total * total))
    return max(0.0, (centre - spread) / denominator)


def _cut_for_accuracy(rows: list[tuple[float, float]], target: float,
                      z: float = 0.0) -> float | None:
    """
    The lowest cut that still reaches `target`, so the most rows kept.

    Walking prefixes of the ranking and remembering the longest one that clears
    the bar. Accuracy over a prefix is not monotonic, so this takes the largest
    that qualifies rather than stopping at the first that fails. Prefixes shorter
    than FLOOR are not eligible: a handful of the judge's surest answers are all
    correct often enough to look like any target you ask for.

    With `z` above zero the prefix has to clear the bar on the low end of a
    confidence interval rather than on the observed rate. That is the difference
    between "these rows scored 95% here" and "these rows will score 95%", and it
    is not a small one: a cut fitted to the observed rate overshoots by however
    much the sample happened to flatter it, which is about half the time.

    Only prefixes a cut can actually produce are eligible. See `_placeable`.
    """
    best: float | None = None
    for certainty, _taken, reached in _rungs(rows, z):
        if reached >= target:
            best = certainty
    return best


def _placeable(ranked: list[tuple[float, float]]) -> list[int]:
    """
    Where a cut may sit: the index ending each run of equal certainties.

    A cut is applied as `certainty >= cut`, so it keeps every answer sharing a
    value or none of them. Judges do not report a smooth spread: measured on one
    provider, 53% of one corpus and 65% of another came back at exactly 1.000.
    Scoring a prefix that stops inside such a run certifies a set that nothing
    can deploy. Before this rule existed, asking for 97% on that second corpus
    returned a cut keeping 65% of the rows at 96.5%, and it did so with the bound
    engaged, because the bound was computed over 500 rows of a block and the cut
    then kept all 2,127 of them.
    """
    return [index for index in range(len(ranked))
            if index + 1 == len(ranked) or ranked[index + 1][0] != ranked[index][0]]


def _rungs(rows: list[tuple[float, float]], z: float):
    """Every cut a threshold could be, with what it reaches. Best first."""
    ranked = sorted(rows, key=lambda row: -row[0])
    running, right = [], 0.0
    for _certainty, correct in ranked:
        right += correct
        running.append(right)
    for index in _placeable(ranked):
        taken = index + 1
        if taken < FLOOR:
            continue
        hat = running[index] / taken
        yield ranked[index][0], taken, (_wilson_lower(hat, taken, z) if z > 0 else hat)


@dataclass(frozen=True)
class Resolution:
    """
    How finely a judge's certainty divides its own work.

    `levels` is how many distinct certainties came back. `share` is how much of
    the pile sits on the single most common one and `crowd_accuracy` is how
    accurate that block is by itself. `reachable` is the most a cut could keep at
    the bar you asked about, or None if you did not ask.
    """

    levels: int
    crowd: float
    share: float
    crowd_accuracy: float
    reachable: float | None = None
    asked: float | None = None

    @property
    def blocked(self) -> bool:
        """Whether the bar you asked for is out of reach at any coverage."""
        return self.reachable is not None and self.reachable <= 0.0

    def __str__(self) -> str:
        lines = [f"{self.levels} distinct certainties, "
                 f"{self.share:.0%} of answers tied at {self.crowd:.3f}",
                 f"  that block is {self.crowd_accuracy:.1%} accurate on its own"]
        if self.asked is not None:
            lines.append(f"  at {self.asked:.0%} a cut can keep at most "
                         f"{self.reachable:.0%} of the pile")
        return "\n".join(lines)


def resolution(answers: Sequence[Answer], gold: Sequence[str],
               accuracy: float | None = None) -> Resolution:
    """
    Whether this judge's certainty is fine-grained enough to cut at all.

    The check that AUROC cannot make. `discrimination` asks whether the ranking
    knows right from wrong, and a judge can pass that and still be impossible to
    threshold, because AUROC averages tied ranks: one that ranks its work
    properly and one that reports a single number for two thirds of it score the
    same. A cut cannot average anything. It keeps a whole block of equal
    certainties or none of it.

    Measured on one provider, 65% of a 3,270 row corpus came back at exactly
    1.000 and that block was 96.5% accurate by itself. Its pooled AUROC was a
    healthy 0.785, and at a 97% bar there was no cut at all, at any coverage,
    even knowing every label in advance.

    When a bar is blocked, the fix is not a cleverer cut or more labels. It is
    more certainty to sort by. Asking each item several times and averaging
    splits the block: on that corpus six asks turned 259 levels out of 61 and
    took coverage at a 97% bar from nothing to 59.8%.

        cal = resolution(answers, labels, accuracy=0.97)
        if cal.blocked:
            print(cal)          # and ask each item more than once
    """
    rows = _scored(answers, gold)
    if not rows:
        raise JevError("no usable answers to measure the resolution of")
    counts: dict[float, int] = {}
    for certainty, _correct in rows:
        counts[certainty] = counts.get(certainty, 0) + 1
    crowd = max(counts, key=lambda value: counts[value])
    block = [correct for certainty, correct in rows if certainty == crowd]
    reachable = None
    if accuracy is not None:
        reachable = max((taken / len(rows) for _c, taken, reached in _rungs(rows, 0.0)
                         if reached >= accuracy), default=0.0)
    return Resolution(levels=len(counts), crowd=crowd,
                      share=counts[crowd] / len(rows),
                      crowd_accuracy=statistics.mean(block),
                      reachable=reachable, asked=accuracy)


# A block bigger than this cannot be the incidental reason a bar was missed.
CROWDED = 0.20


def _crowding(rows: list[tuple[float, float]], accuracy: float) -> Resolution | None:
    """
    The resolution reading, but only when it is why the bar was missed.

    Returns None unless one certainty holds a large share of the pile *and* that
    block on its own fails the bar. Both halves matter: a big block that clears
    the bar comfortably is not a problem, and a small one is not what stopped a
    cut being found.
    """
    counts: dict[float, int] = {}
    for certainty, _correct in rows:
        counts[certainty] = counts.get(certainty, 0) + 1
    crowd = max(counts, key=lambda value: counts[value])
    share = counts[crowd] / len(rows)
    if share < CROWDED:
        return None
    block = [correct for certainty, correct in rows if certainty >= crowd]
    if statistics.mean(block) >= accuracy:
        return None
    return Resolution(levels=len(counts), crowd=crowd, share=share,
                      crowd_accuracy=statistics.mean(block),
                      reachable=0.0, asked=accuracy)


# Below this a class has too few answers to say anything about, so the gate says
# so rather than reading noise. Set where the interval stops being actionable:
# at n=175 it already spans 0.64 to 1.00.
FEW_IN_CLASS = 100


@dataclass(frozen=True)
class Verdict:
    """What to do with the answers a judge filed under one label."""

    label: str
    count: int
    accuracy: float
    floor: float                      # accuracy on a lower bound, not the estimate
    discrimination: float
    low: float
    high: float
    cut: float | None
    coverage: float
    action: str                       # take, cut, send, inverted, unknown
    why: str

    def __str__(self) -> str:
        known = self.discrimination == self.discrimination
        area = f"{self.discrimination:.3f}" if known else "  -  "
        return (f"says {self.label:<12} n={self.count:<6} {self.accuracy:>6.1%} right   "
                f"AUROC {area}   {self.action:<9} {self.why}")


def routing(answers: Sequence[Answer], gold: Sequence[str], *,
            accuracy: float, confidence: float = 0.0) -> list[Verdict]:
    """
    Which of this judge's own answers you can trust, one verdict per label.

        for verdict in routing(answers, labels, accuracy=0.95):
            print(verdict)

    One cut over everything assumes the certainty means the same thing whatever
    the judge said, and it does not. Split by the label it output, which is the
    only one of the two you have at inference time, and the three questions come
    apart: a class can already clear your bar and need no cut, or be unsortable
    and need a person, or have its certainty pointing **backwards**.

    Measured on a shipped model over 1,347 rows, its own three labels:

        says refusal      n=185   97.8% right   AUROC 0.993
        says compliance   n=971   66.4% right   AUROC 0.404   inverted
        says partial      n=191    6.3% right   no signal

    Pooled, that is 62% and AUROC 0.540, which reads as a judge not worth using.
    Per label it is one class you can automate outright, one you must never
    threshold, and one to send to a person. Nothing in the pooled number
    survives contact with that, and `calibrate` returns the pooled number.

    `inverted` is the verdict worth staring at. It means more confident is more
    wrong inside that class, so triaging on certainty there keeps precisely the
    answers you would most want caught. It usually means a systematic confusion
    rather than noise, and no threshold fixes it.
    """
    if not 0.0 < accuracy < 1.0:
        raise JevError(f"accuracy has to be between 0 and 1, not {accuracy}")
    z = _z_for(confidence)
    if len(answers) != len(gold):
        raise JevError(f"{len(answers)} answers and {len(gold)} labels; they have to line up")

    grouped: dict[str, list[tuple[float, float]]] = {}
    for answer, want in zip(answers, gold, strict=True):
        if answer.ok:
            grouped.setdefault(answer.label, []).append(
                (answer.certainty, 1.0 if answer.label == want else 0.0))
    if not grouped:
        raise JevError("no usable answers to route")

    out = []
    for label, rows in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        got = statistics.mean(correct for _certainty, correct in rows)
        floor = _wilson_lower(got, len(rows), z) if z > 0 else got
        area, low, high = _auroc_interval(rows)
        cut = _cut_for_accuracy(rows, accuracy, z)
        coverage = _at(rows, cut)[0] if cut is not None else 0.0

        if len(rows) < FEW_IN_CLASS:
            action, why = "unknown", f"{len(rows)} answers is too few to judge this class on"
        elif floor >= accuracy:
            action, why = "take", (f"already clears {accuracy:.0%} on its own"
                                   f"{f' lower bound, {floor:.1%}' if z > 0 else f', {floor:.1%}'}")
        elif high == high and high < 0.5:
            action, why = "inverted", (f"certainty runs backwards here, AUROC up to {high:.3f}. "
                                       f"Do not cut on it")
        elif area != area or low != low or low <= 0.5 or area < MIN_SIGNAL:
            action, why = "send", "the certainty cannot sort this class; send all of it"
        elif cut is None or coverage <= 0:
            action, why = "send", f"it sorts, but no cut in it reaches {accuracy:.0%}"
        else:
            action, why = "cut", f"at {cut:.3f}, keeping {coverage:.0%} of the class"
        out.append(Verdict(label=label, count=len(rows), accuracy=got, floor=floor,
                           discrimination=area, low=low, high=high, cut=cut,
                           coverage=coverage, action=action, why=why))
    return out


def _at(rows: list[tuple[float, float]], cut: float) -> tuple[float, float]:
    """Coverage and accuracy over everything at or above `cut`, as triage would."""
    kept = [correct for certainty, correct in rows if certainty >= cut]
    if not kept:
        return 0.0, 0.0
    return len(kept) / len(rows), statistics.mean(kept)


def _work(answers: list[Answer], gold: list[str], keep: float | None,
          accuracy: float | None, splits: int, seed: int,
          min_signal: float, confidence: float = 0.0) -> Calibration:
    if (keep is None) == (accuracy is None):
        raise JevError("say one of keep or accuracy, not both and not neither")
    if confidence and keep is not None:
        raise JevError("confidence applies to accuracy=, not keep=: with keep the cut is set "
                       "by the share you asked to keep, so there is no bar to be confident about")
    z = _z_for(confidence)
    for name, value in (("keep", keep), ("accuracy", accuracy)):
        if value is not None and not 0.0 < value <= 1.0:
            raise JevError(f"{name} is a fraction above 0 and up to 1")

    rows = _scored(answers, gold)
    if len(rows) < FLOOR:
        raise JevError(f"{len(rows)} usable answers is too few to find a cut in; "
                       f"{FEW} labelled rows is a sensible floor and {FLOOR} is the hard one")

    notes: list[str] = []
    skipped = len(answers) - len(rows)
    if skipped:
        notes.append(f"{skipped} answers came back unusable and are not in this; "
                     f"triage always puts those in the review pile")
    if len(rows) < FEW:
        notes.append(f"{len(rows)} rows is thin. The interval below is the width of that, "
                     f"and {FEW} or more would narrow it")

    baseline = statistics.mean(correct for _c, correct in rows)

    # Before asking where to cut, ask whether a cut can do anything here. A
    # threshold sorts by certainty and keeps the top, so if the certainty cannot
    # tell a right answer from a wrong one there is nothing to sort, and the
    # honest reply is not a number, it is no.
    separates = discrimination(answers, gold)
    if separates < min_signal:
        raise JevError(
            f"this judge's certainty separates right answers from wrong ones at {separates:.3f}, "
            f"where 0.5 is a coin flip and anything under {min_signal:.2f} is treated as none. "
            f"A threshold sorts by certainty, so on these rows there is nothing for one to find, "
            f"and a gain would be the sample flattering itself. It agrees {baseline:.1%} of the "
            f"time overall. Either this judge cannot do this task or the question needs "
            f"rewording; pass min_signal lower to get the number anyway.")

    # The cut to deploy is fitted on everything, because that is the one that has
    # seen the most of your data.
    if keep is not None:
        cut = _cut_for_keep(rows, keep)
    else:
        found = _cut_for_accuracy(rows, accuracy, z)
        if found is None:
            # Reported on the same footing the search used, or the message
            # contradicts itself: with a bound in play the best observed rate can
            # read 100% while nothing at all is certifiable.
            best = max((reached for _c, _t, reached in _rungs(rows, z)), default=0.0)
            # Which of the two reasons it is, because the fixes are opposite. A
            # pile crowded onto one certainty cannot be cut however many labels
            # arrive, and telling that caller to label more rows wastes their
            # afternoon. Asking each item more than once is what helps there.
            crowded = _crowding(rows, accuracy)
            if crowded is not None:
                raise JevError(
                    f"no cut reaches {accuracy:.0%} on these rows, and more labels will not "
                    f"change that. {crowded.share:.0%} of the answers are tied at "
                    f"{crowded.crowd:.3f} and that block is {crowded.crowd_accuracy:.1%} "
                    f"accurate on its own, so a cut either keeps all of it or none of it and "
                    f"neither clears the bar. The judge's certainty is too coarse here, not too "
                    f"weak: it reported {crowded.levels} distinct values over {len(rows):,} "
                    f"rows. Ask each item several times and average, which splits the block, or "
                    f"ask for less than {accuracy:.0%}")
            if z > 0:
                raise JevError(
                    f"no cut clears {accuracy:.0%} with {confidence:.0%} confidence on these "
                    f"rows. The best any of them can guarantee over at least {FLOOR} kept is "
                    f"{best:.1%}, and the observed rate is higher than that. At {len(rows):,} "
                    f"rows the binding constraint is usually the count rather than the judge, "
                    f"since a bound this tight needs more of them. Ask for less, lower the "
                    f"confidence, or label more rows")
            raise JevError(
                f"no cut reaches {accuracy:.0%} on these rows; the best any of them manages "
                f"over at least {FLOOR} kept is {best:.1%}. Ask for less, or the judge is not "
                f"good enough at this question to be trusted at that bar")
        cut = found

    # What the safety margin is costing, if one was asked for. Wide means the
    # bound is what is holding coverage back and more labels would move it;
    # narrow means the data has already given up everything it has.
    headroom = 0.0
    if z > 0 and accuracy is not None:
        loose = _cut_for_accuracy(rows, accuracy, 0.0)
        if loose is not None:
            headroom = max(0.0, _at(rows, loose)[0] - _at(rows, cut)[0])

    # What to expect from it is measured on rows it was not chosen on, many times,
    # because one split of a few hundred rows is mostly luck.
    shaker = random.Random(seed)
    coverages, scores = [], []
    order = list(range(len(rows)))
    for _ in range(max(1, splits)):
        shaker.shuffle(order)
        half = len(order) // 2
        fit = [rows[index] for index in order[:half]]
        test = [rows[index] for index in order[half:]]
        if keep is not None:
            edge = _cut_for_keep(fit, keep)
        else:
            edge = _cut_for_accuracy(fit, accuracy, z)
            if edge is None:
                continue
        covered, scored = _at(test, edge)
        if covered:
            coverages.append(covered)
            scores.append(scored)

    if not scores:                       # every split failed to find a cut
        notes.append("no held-out split could find this cut, so there is no estimate "
                     "of what it does on rows it has not seen")
        return Calibration(cut, *_at(rows, cut), 0.0, 0.0, baseline,
                           len(rows), 0, separates, headroom, tuple(notes))

    scores.sort()
    edge = max(0, int(len(scores) * 0.05) - 1) if len(scores) >= 20 else 0
    low, high = scores[edge], scores[len(scores) - 1 - edge]

    # A judge that is sure of nearly everything leaves the ranking nothing to
    # sort by, and the coverage it settles on then has little to do with what was
    # asked for. Seen on AG News, where two judgements in three come back at 1.000.
    grain = resolution(answers, gold)
    if grain.share >= CROWDED:
        notes.append(f"{grain.share:.0%} of the answers tie at {grain.crowd:.3f} and that "
                     f"block is {grain.crowd_accuracy:.1%} accurate on its own, so a cut "
                     f"keeps all of it or none of it. {grain.levels} distinct certainties "
                     f"came back, which is the ceiling on how finely you can cut. Asking "
                     f"each item several times and averaging splits the block")
    if baseline >= (accuracy or 0.0) and accuracy is not None:
        notes.append(f"the judge already agrees {baseline:.1%} of the time without any cut, "
                     f"so you may not need one")
    # One cut assumes the certainty means the same thing whatever the judge
    # said, and on a real model it did not: 0.993 inside one of its labels and
    # 0.404 inside another, pooling to 0.540. A caller who never reaches for
    # `routing` should still be told that the number here is an average over
    # classes that disagree about which direction certainty points.
    if accuracy is not None:
        split = routing(answers, gold, accuracy=accuracy, confidence=confidence)
        backwards = [v for v in split if v.action == "inverted"]
        if backwards:
            worst = max(backwards, key=lambda v: v.count)
            share = worst.count / sum(v.count for v in split)
            notes.append(
                f"on the {share:.0%} of answers it filed under {worst.label!r} the certainty "
                f"runs backwards, AUROC {worst.discrimination:.3f}, so the cut above keeps "
                f"the ones you would most want caught. See routing() for a verdict per label")
        elif len(split) > 1:
            known = [v for v in split if v.discrimination == v.discrimination]
            if known and max(v.discrimination for v in known) - \
                    min(v.discrimination for v in known) >= 0.25:
                notes.append(
                    "the certainty is far better inside some of this judge's labels than "
                    "others, so one cut over all of them is an average of unlike things. "
                    "See routing()")
    # The cut was fitted on every row and is then being asked about rows it has
    # not seen, so it flatters itself. Saying by how much is the whole reason the
    # held-out halves are here, and quietly handing back a number under the one
    # that was asked for would waste them.
    got = statistics.median(scores)
    if accuracy is not None and got < accuracy:
        notes.append(f"you asked for {accuracy:.1%} and rows the cut had not seen came in at "
                     f"{got:.1%}. Fitting on everything flatters the cut by about that much, "
                     f"so the held-out figure is the one to plan with")
    held = statistics.median(coverages)
    if accuracy is not None and held < 0.25:
        notes.append(f"that bar is only reachable on {held:.0%} of the rows, so most of the "
                     f"pile still goes to a person. A lower one buys back a lot of coverage")

    return Calibration(cut=cut, coverage=statistics.median(coverages),
                       accuracy=statistics.median(scores), low=low, high=high,
                       baseline=baseline, items=len(rows), splits=len(scores),
                       discrimination=separates, headroom=headroom, notes=tuple(notes))
