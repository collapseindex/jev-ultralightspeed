"""The shape of a request, and the one rule about what may go in it."""

from __future__ import annotations

from typing import Sequence

from ._answers import _Ask
from ._errors import JevError
from ._settings import MAX_ITEM_CHARS


def _question(instructions: str, criteria: dict | None, options: dict | None,
              levels: Sequence[str] | None = None) -> dict:
    if levels:
        # An ordered list, low to high, and the answer is a position along it
        # rather than one of them.
        return {"type": "score", "instructions": instructions,
                "criteria": [str(level) for level in levels]}
    if options:
        return {"type": "choice", "instructions": instructions,
                "criteria": {str(k): str(v) for k, v in options.items()}}
    question = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = {str(k): str(v) for k, v in criteria.items()}
    return question


GUIDANCE = "guidance"                  # the state key the shared question lives under


def _guidance_text(instructions: str, criteria: dict | None, options: dict | None,
                   levels: Sequence[str] | None = None) -> str:
    """
    The question as one piece of text, to sit in the state once.

    For a pick-one or a score question the options and the levels stay in each
    question, because they are the answer space rather than wording. Only the
    instructions move.
    """
    if options or levels:
        return instructions
    lines = [instructions]
    for name, meaning in (criteria or {}).items():
        lines.append(f"{name}: {meaning}")
    return "\n".join(lines)


def _one_body(model, ask: "_Ask") -> dict:
    return {"model": model, "state": {"item_1": ask.text},
            "questions": {"item_1": _question(ask.instructions, ask.criteria, ask.options,
                                              ask.levels)}}


def _same_question(asks: Sequence["_Ask"]) -> bool:
    """
    Whether every ask in a pack is asking the same thing.

    By identity, not by value: the questions in a call are copied once at the
    start, so rows sharing a question share the object, and comparing dicts for
    every pack would cost more than it saves.
    """
    first = asks[0]
    return all(ask.instructions is first.instructions
               and ask.criteria is first.criteria
               and ask.options is first.options
               and ask.levels is first.levels for ask in asks[1:])


def _packed_body(model, asks: Sequence["_Ask"], guidance: str = "repeat") -> dict:
    """
    Several items in one state, one question each, every question naming the
    item it is about.

    The questions can differ, because the API has one per item and always did.
    Usually they do not, and then the only thing that changes between them is
    that name.

    With `guidance="once"` the question's wording moves into the state under one
    key and each question points at it. Measured on a live 32-item request with a
    long yes/no question, that took the billed input from 4,598 tokens to 1,777,
    because repeating the whole question thirty-two times is most of the body. It
    is a different prompt, so it is not the default, and it needs every item in
    the pack to be asking the same thing before there is anything to share.
    """
    state = {f"item_{position}": ask.text for position, ask in enumerate(asks, start=1)}
    shared = guidance == "once" and _same_question(asks)
    if shared:
        state[GUIDANCE] = _guidance_text(asks[0].instructions, asks[0].criteria,
                                         asks[0].options, asks[0].levels)
    questions = {}
    for position, ask in enumerate(asks, start=1):
        name = f"item_{position}"
        if shared:
            questions[name] = _question(
                f"Judge {name} only, ignoring every other item, "
                f"against the question in {GUIDANCE}.",
                None, ask.options, ask.levels)
        else:
            questions[name] = _question(
                f"{ask.instructions} Judge {name} only, ignoring every other item.",
                ask.criteria, ask.options, ask.levels)
    return {"model": model, "state": state, "questions": questions}


def _clean(item: str, index: int = 0) -> str:
    text = item if isinstance(item, str) else str(item)
    if len(text) > MAX_ITEM_CHARS:
        raise JevError(f"item {index + 1} is {len(text):,} characters, over the "
                       f"{MAX_ITEM_CHARS:,} limit; split it first. Nothing has been sent.")
    return text
