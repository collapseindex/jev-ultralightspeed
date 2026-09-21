"""What an answer is, what a run cost, and which answers to trust."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from ._errors import JevError


@dataclass
class Answer:
    """One item's answer: the probability, and what it works out to."""

    item: str
    # The judge's own probability for the answer it gave. It ranks well and it is
    # not a probability of being right: measured against human labels it runs
    # about 4 points over in the middle of the range. Sort by it, do not read it
    # as a percentage. See `triage`.
    p: float
    label: str                    # "yes" or "no", or the chosen option
    kind: str = "noul"
    distribution: dict = field(default_factory=dict)
    confidence: float | None = None
    position: int = 1             # where this item sat in its request, 1 for an unpacked one
    packed: int = 1               # how many items shared that request
    error: str | None = None      # why there is no answer here, with on_error="skip"
    # Where it landed along the levels of a `score` question, and it can land
    # between two of them: 1.05 on ["Calm", "Frustrated", "Very angry"] is
    # frustrated with a little anger behind it. None for the other two types.
    score: float | None = None

    @property
    def yes(self) -> bool:
        return self.label == "yes"

    @property
    def ok(self) -> bool:
        """False for a skipped item. Check this before trusting `label`."""
        return self.error is None

    @property
    def certainty(self) -> float:
        """
        How sure the judge is of the answer it actually gave, which is not `p`.

        For a yes/no question `p` is the probability of **yes**, so 0.01 is a very
        confident no. Sorting by `p` there throws out the answers it is surest
        about first, which is what `triage` used to do. For a pick-one question
        `p` is already the chosen option's own probability, so the two agree.
        """
        if not self.ok:
            return 0.0
        if self.kind == "noul":
            return max(self.p, 1.0 - self.p)
        return self.p                 # choice and score: the level it settled on


@dataclass
class Ask:
    """
    One item, and the question to ask about it.

    For `judge()`, when the question is not the same for every row. Anything left
    out falls back to what the call was given, so `Ask("some text")` is an
    ordinary item and `Ask(text, options={...})` is one with its own answer space.

    `levels` makes it a score question: an ordered list, low to high, and the
    answer lands somewhere along it rather than on one of them.
    """

    item: str
    instructions: str | None = None
    criteria: dict | None = None
    options: dict | None = None
    levels: Sequence[str] | None = None


class _Ask(NamedTuple):
    """An ask with its defaults already filled in, which is what the engine sees."""

    text: str
    instructions: str
    criteria: dict | None
    options: dict | None
    levels: Sequence[str] | None = None


@dataclass
class Usage:
    """What a run cost, so a claim about speed can be checked."""

    items: int = 0
    requests: int = 0            # requests that came back with an answer
    retries: int = 0             # attempts that failed and were sent again
    pushback: dict = field(default_factory=dict)   # what the retries were, by status code
    waited: float = 0.0          # seconds spent sitting out a backoff
    input_tokens: int = 0
    output_tokens: int = 0
    cached: int = 0
    resumed: int = 0             # answered by a checkpoint from an earlier run
    skipped: int = 0             # items with no readable answer, under on_error="skip"
    seconds: float = 0.0

    # TypeSafe publishes a price per million input tokens and this counts those.
    # Output is about twenty tokens an item, measured, roughly 5% of the total; if
    # it is billed separately then `usd` is low by whatever that costs.
    USD_PER_MILLION_INPUT = 0.042

    @property
    def answered(self) -> int:
        """Items that actually cost a request. The rest came from memory or disk."""
        return max(0, self.items - self.cached - self.resumed)

    @property
    def items_per_second(self) -> float:
        # Over the items that were asked, not the ones handed back for free: a
        # resumed run otherwise reports millions of items a second, which is the
        # one number in here that would not be true.
        return self.answered / self.seconds if self.seconds and self.answered else 0.0

    @property
    def tokens_per_item(self) -> float:
        return ((self.input_tokens + self.output_tokens) / self.answered
                if self.answered else 0.0)

    @property
    def usd(self) -> float:
        return self.input_tokens * self.USD_PER_MILLION_INPUT / 1e6

    @property
    def why_retried(self) -> str:
        """The retries by status, so a slow run can say what slowed it."""
        if not self.pushback:
            return ""
        parts = [f"{count}x{'transport' if status == 0 else status}"
                 for status, count in sorted(self.pushback.items())]
        return f"{', '.join(parts)}, {self.waited:.1f}s waiting"

    def __str__(self) -> str:
        retried = f", {self.retries} retried" if self.retries else ""
        retried += f", {self.resumed} resumed" if self.resumed else ""
        retried += f", {self.skipped} skipped" if self.skipped else ""
        retried += f" ({self.why_retried})" if self.pushback else ""
        asked = "" if self.answered == self.items else f" of {self.items}"
        return (f"{self.answered}{asked} items in {self.seconds:.2f}s "
                f"({self.items_per_second:.1f}/s, {self.requests} requests{retried}, "
                f"{self.tokens_per_item:.0f} tokens/item, ${self.usd:.5f})")


def triage(answers: Sequence[Answer], *, keep: float | None = None,
           at_least: float | None = None) -> tuple[list[Answer], list[Answer]]:
    """
    Split answers into the ones worth trusting and the ones worth a look.

    Say either how much to keep or where to cut:

        trusted, review = triage(answers, keep=0.8)
        trusted, review = triage(answers, at_least=0.92)

    Ranked by `Answer.certainty`, which for a yes/no question is not `p`: `p` is
    the probability of yes, so a confident no has a low one. `keep` hands back
    exactly the share asked for even when scores tie.

    This is the one lever that moves agreement, and it moves it a long way. On
    the 1,347 human-labelled completions in dinostomp's `xstest-refusal` pod,
    with the cut chosen on one half and measured on the other:

        keep 100%   89.7%
        keep  89%   94.4%
        keep  81%   96.8%
        keep  72%   98.5%

    The two annotators who labelled that pod agreed with each other 97.3% of the
    time, so the fifth it is least sure about is carrying most of the difference
    between this judge and a person. Both lists keep the order they came in, and
    a skipped answer is always one to look at.

    Where to cut is a property of your question and your items, not of this
    library, so measure it on a few hundred labelled rows of your own.
    `bench_confidence.py` is that measurement.
    """
    if (keep is None) == (at_least is None):
        raise JevError("say one of keep or at_least, not both and not neither")
    usable = [index for index, answer in enumerate(answers) if answer.ok]
    if at_least is not None:
        chosen = {index for index in usable if answers[index].certainty >= at_least}
    else:
        if not 0.0 <= keep <= 1.0:
            raise JevError("keep is a fraction between 0 and 1")
        # Exactly the share asked for, and ties broken by where the answer came
        # in. Taking everything at or above the cut hands back all ten of ten
        # answers that scored the same when two were wanted.
        ranked = sorted(usable, key=lambda index: (-answers[index].certainty, index))
        chosen = set(ranked[:int(len(usable) * keep)])
    trusted = [answer for index, answer in enumerate(answers) if index in chosen]
    review = [answer for index, answer in enumerate(answers) if index not in chosen]
    return trusted, review


def _copy_answer(answer: "Answer", text: str) -> "Answer":
    """
    The same answer for another copy of the text, sharing nothing mutable.

    `error` travels with it. Left behind, a second copy of a skipped item came
    back reporting `ok` True with nothing in it, which is the worst of the three
    possible answers.
    """
    return Answer(item=text, p=answer.p, label=answer.label, kind=answer.kind,
                  distribution=dict(answer.distribution), confidence=answer.confidence,
                  position=answer.position, packed=answer.packed, error=answer.error,
                  score=answer.score)


def _read(entry: dict, text: str, position: int = 1, packed: int = 1) -> Answer:
    if "noul" in entry:
        p = float(entry["noul"])
        return Answer(item=text, p=p, label="yes" if p >= 0.5 else "no", kind="noul",
                      distribution={"yes": p, "no": 1.0 - p},
                      confidence=_float_or_none(entry.get("confidence")),
                      position=position, packed=packed)
    if "score" in entry:
        # The levels come back numbered, with a legend naming them. The names are
        # what the caller wrote, so the distribution is handed back under those
        # rather than under "0", "1", "2".
        legend = {str(k): str(v) for k, v in (entry.get("legend") or {}).items()}
        numbered = {str(k): float(v) for k, v in (entry.get("probabilities") or {}).items()}
        distribution = {legend.get(index, index): value for index, value in numbered.items()}
        value = float(entry["score"])
        highest = max(int(index) for index in numbered) if numbered else 0
        nearest = str(min(max(0, round(value)), highest))
        return Answer(item=text, p=numbered.get(nearest, 0.0),
                      label=legend.get(nearest, nearest), kind="score",
                      distribution=distribution, score=value,
                      confidence=_float_or_none(entry.get("confidence")),
                      position=position, packed=packed)
    if "choice" in entry:
        distribution = {str(k): float(v) for k, v in (entry.get("probabilities") or {}).items()}
        label = str(entry["choice"])
        return Answer(item=text, p=distribution.get(label, 0.0), label=label, kind="choice",
                      distribution=distribution,
                      confidence=_float_or_none(entry.get("confidence")),
                      position=position, packed=packed)
    raise JevError(f"unreadable answer: {list(entry)[:5]}")


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
