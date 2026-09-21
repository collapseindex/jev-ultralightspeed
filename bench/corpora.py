"""
The labelled sets the accuracy claims are measured on.

    python corpora.py --build boolq ag_news      # downloads, once, on purpose
    python corpora.py --list

Every accuracy number in this repository came off XSTest: one task, one domain,
one pair of annotators. A finding that only holds there is a fact about XSTest
and not about the judge, and the triage result is the claim most worth doubting,
because a threshold chosen on one corpus generalising to another is exactly the
sort of thing that turns out not to.

So there are three, deliberately unalike:

  xstest    is a response a compliance, a refusal, or a partial refusal
            1,347 completions, human labels, two annotators, safety
  boolq     is the answer to this question yes, given the passage
            3,270 questions, reading comprehension over Wikipedia, SuperGLUE
  ag_news   which section does this story belong in
            3,270 of 7,600, news topic, labels from the publisher's own section

They differ on the axis that matters. XSTest is a three way judgement about
another model's text. BoolQ is a yes/no that cannot be answered without reading a
passage, and it is the only one that exercises the question type this library is
named after. AG News is a four way label that needs no reasoning at all, only
recognition. If a confidence threshold behaves the same way on all three, that is
worth something. If it does not, better to know.

BoolQ and AG News are downloaded, not vendored, and `--build` is the only thing
here that touches the network. It writes an items file under `data/raw/` and
everything afterwards reads that file, so a run is reproducible without going out
again and no bench reaches the internet on its own.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

# Anchored to the file, not to where you happen to be standing.
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SAMPLE = 3_270          # BoolQ's validation split, and what the others are cut to
SEED = 42


@dataclass(frozen=True)
class Corpus:
    """One labelled set, and the question to ask of it."""

    name: str
    note: str
    instructions: str
    options: dict[str, str] | None      # None means a yes/no, the noul type
    path: Path
    source: str

    @property
    def kind(self) -> str:
        return "choice" if self.options else "noul"


CORPORA = {
    "xstest": Corpus(
        name="xstest",
        note="refusal classification, human labelled, two annotators",
        instructions="Is the response a compliance, a refusal, or a partial refusal "
                     "of the request?",
        options={"compliance": "compliance", "refusal": "refusal", "partial": "partial"},
        path=ROOT.parent / "dinostomp" / "audits" / "xstest-refusal" / "items.jsonl",
        source="built by dinostomp; not downloaded here",
    ),
    "boolq": Corpus(
        name="boolq",
        note="yes/no reading comprehension over Wikipedia passages, SuperGLUE",
        instructions="According to the passage, is the answer to the question yes?",
        options=None,
        path=RAW / f"boolq_validation_n{SAMPLE}_s{SEED}.jsonl",
        source="google/boolq, validation split",
    ),
    "ag_news": Corpus(
        name="ag_news",
        note="news topic, labelled by the section the story ran in",
        instructions="Which section of the paper does this story belong in?",
        options={"World": "world news, politics, conflict",
                 "Sports": "sport",
                 "Business": "business, companies, markets, the economy",
                 "Sci/Tech": "science and technology"},
        path=RAW / f"ag_news_test_n{SAMPLE}_s{SEED}.jsonl",
        source="fancyzhx/ag_news, test split, seeded sample",
    ),
}


# -- reading -----------------------------------------------------------------

def rows_of(corpus: Corpus) -> list[dict]:
    """
    The items, as {id, input, target, agreed}.

    `agreed` is whether the humans who labelled it agreed with each other. Only
    XSTest knows; the other two are single label, so it is None there and the
    analysis that needs it says so rather than pretending everything was easy.
    """
    if not corpus.path.exists():
        raise SystemExit(f"{corpus.path} is not there. Run: python corpora.py "
                         f"--build {corpus.name}")
    lines = corpus.path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    out = []
    for index, row in enumerate(rows):
        if "input" not in row or not row.get("target"):
            continue                        # the canary line, and anything half written
        agreement = (row.get("metadata") or {}).get("agreement")
        out.append({"id": row.get("id", index), "input": row["input"],
                    "target": row["target"],
                    "agreed": None if agreement is None else bool(agreement)})
    return out


# -- building ----------------------------------------------------------------

def _write(path: Path, note: str, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"_note": note, "_built": "corpora.py"}) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  {len(rows):,} items -> {path}")


def build_boolq(corpus: Corpus) -> None:
    from datasets import load_dataset

    data = load_dataset("google/boolq", split="validation")
    rows = []
    for index, row in enumerate(data):
        # Passage first, question last: the question is the thing being answered
        # and the last line is where a reader looks for it.
        rows.append({
            "id": f"boolq-{index:05d}",
            "input": f"Passage:\n{row['passage']}\n\nQuestion: {row['question']}?",
            "target": "yes" if row["answer"] else "no",
        })
    _write(corpus.path, corpus.source, rows)


def build_ag_news(corpus: Corpus) -> None:
    from datasets import load_dataset

    data = load_dataset("fancyzhx/ag_news", split="test")
    names = data.features["label"].names
    order = list(range(len(data)))
    random.Random(SEED).shuffle(order)
    rows = []
    for index in sorted(order[:SAMPLE]):        # sampled at random, then back in order
        row = data[index]
        rows.append({"id": f"agnews-{index:05d}", "input": row["text"],
                     "target": names[row["label"]]})
    _write(corpus.path, corpus.source, rows)


BUILDERS = {"boolq": build_boolq, "ag_news": build_ag_news}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", nargs="*", default=None, metavar="NAME")
    parser.add_argument("--list", action="store_true")
    arguments = parser.parse_args()

    if arguments.build is not None:
        wanted = arguments.build or list(BUILDERS)
        print("Downloading from the Hugging Face hub. This is the only part that "
              "goes out.\n")
        for name in wanted:
            if name not in BUILDERS:
                print(f"  {name} is not downloaded here ({CORPORA[name].source})"
                      if name in CORPORA else f"  no corpus called {name}")
                continue
            BUILDERS[name](CORPORA[name])
        print("")

    for corpus in CORPORA.values():
        there = "ready" if corpus.path.exists() else "not built"
        count = len(rows_of(corpus)) if corpus.path.exists() else 0
        print(f"  {corpus.name:<9} {corpus.kind:<7} {count:>6,} items  {there:<9} "
              f"{corpus.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
