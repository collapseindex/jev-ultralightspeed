"""
Does the judge know when it is wrong?

    python bench_confidence.py --corpus xstest
    python bench_confidence.py --corpus boolq
    python bench_confidence.py --analyse data/results/<the file it wrote>

Throughput is within a fifth of its arithmetic ceiling. Agreement is eight points
below the human one: 89.3% against annotators who agreed with each other on 1,310
of 1,347 completions. So the question worth money is not how to make the judge
better, it is whether the judge's own numbers say which verdicts to trust.

If they do, the useful claim stops being "89% on everything" and becomes "97% on
the nine tenths it is sure about, and here is the tenth to look at yourself".

That was measured on one corpus, which is the weakness in it. A threshold chosen
on one task generalising to another is exactly the sort of claim that turns out
to be a fact about the task, so the same run now goes against three, picked to be
unalike (see `corpora.py`). The analysis is identical for each and the numbers
are meant to be read side by side.

Collecting costs about 12 cents a corpus. It writes every judgement to a file so
the analysis can be argued with for free afterwards, which is the point of
splitting the two.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# Anchored to the file, not to where you happen to be standing.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import corpora  # noqa: E402

from jev_ultralightspeed import Client  # noqa: E402

OUT = ROOT / "data" / "results"
PACK = 32
COPIES = 6
SEED = 7
COVERAGES = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.3)
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01))


# -- collecting --------------------------------------------------------------

def collect(corpus: corpora.Corpus) -> Path:
    rows = corpora.rows_of(corpus)
    texts, gold, source, agreed = [], [], [], []
    for index, row in enumerate(rows):
        for copy in range(COPIES):
            texts.append(f"{row['input']}\n\n(case {index:05d}-{copy})")
            gold.append(row["target"])
            source.append(index)
            # Whether the humans who labelled it agreed with each other, which is
            # the only honest way to read a judge's mistakes: some of them are on
            # items the humans could not agree about either. None where the
            # corpus is single label and cannot say.
            agreed.append(row["agreed"])
    order = list(range(len(texts)))
    random.Random(SEED).shuffle(order)

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = OUT / (f"{stamp}_confidence_jev-latest_{corpus.name}_{len(rows)}x{COPIES}"
                  f"_p{PACK}_s{SEED}.jsonl")

    client = Client(pack=PACK, workers=8, cache=False, dedupe=False)
    client.warm()
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "jev-confidence-run", "pack": PACK,
                                 "copies": COPIES, "seed": SEED,
                                 "corpus": corpus.name, "question": corpus.kind,
                                 "instructions": corpus.instructions,
                                 "items": str(corpus.path), "when": stamp}) + "\n")
        stream = client.stream((texts[i] for i in order), corpus.instructions,
                               options=corpus.options, chunk=4_000)
        for position, answer in enumerate(stream):
            which = order[position]
            handle.write(json.dumps({
                "source": source[which], "gold": gold[which], "agreed": agreed[which],
                "label": answer.label, "p": answer.p,
                # How sure it is of the answer it gave, which is not p on a yes/no:
                # p near zero there is a confident no, and ranking on p would put
                # the judge's firmest verdicts at the bottom of the pile.
                "certainty": answer.certainty,
                "confidence": answer.confidence,
                "distribution": answer.distribution,
                "position": answer.position, "packed": answer.packed,
            }) + "\n")
            written += 1
            if written % 2000 == 0:
                print(f"  {written:,} of {len(texts):,}")
    client.close()
    print(f"\n{written:,} judgements written to {path}")
    print(f"cost {client.usage}")
    return path


# -- analysing ---------------------------------------------------------------

def read(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line]


def accuracy_of(records) -> float:
    return statistics.mean(1.0 if r["label"] == r["gold"] else 0.0 for r in records)


def certainty_of(record: dict) -> float:
    """
    How sure the judge was of the answer it gave.

    On a pick-one this is just `p`. On a yes/no it is the distance from the coin
    flip, because `p` there is the probability of yes and a firm no sits at the
    bottom of it. Runs written before this was recorded are read the same way, so
    the older files still analyse.
    """
    if record.get("certainty") is not None:
        return record["certainty"]
    return max(record["p"], 1.0 - record["p"]) if record.get("kind") == "noul" else record["p"]


def by_item(records) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["source"]].append(record)
    return grouped


def item_accuracy(records) -> float:
    """Per completion first, so 6 repeats of one item are not 6 pieces of evidence."""
    grouped = by_item(records)
    return statistics.mean(accuracy_of(rows) for rows in grouped.values())


def bootstrap(values: list[float], draws: int = 4_000) -> tuple[float, float]:
    shaker = random.Random(11)
    means = sorted(statistics.mean(shaker.choices(values, k=len(values))) for _ in range(draws))
    return means[int(draws * 0.025)], means[int(draws * 0.975)]


def risk_coverage(records, key: str) -> None:
    """Accuracy over the most confident slice, at several sizes."""
    reading = certainty_of if key == "certainty" else (lambda r: r[key])
    usable = [r for r in records if r.get(key) is not None or key == "certainty"]
    if not usable:
        print(f"  no {key} in this run")
        return
    ranked = sorted(usable, key=reading, reverse=True)
    print(f"  by {key}:")
    print(f"    {'kept':>6}  {'judgements':>10}  {'agreement':>9}  {'cut at':>7}")
    for coverage in COVERAGES:
        keep = ranked[:max(1, int(len(ranked) * coverage))]
        edge = reading(keep[-1])
        print(f"    {coverage:>5.0%}  {len(keep):>10,}  {accuracy_of(keep):>8.1%}  {edge:>7.3f}")


def calibration(records) -> None:
    """What the probability said against what happened."""
    print("  the probability against the outcome:")
    print(f"    {'band':>12}  {'judgements':>10}  {'said':>6}  {'happened':>8}  {'gap':>6}")
    weighted = 0.0
    total = 0
    for low, high in BUCKETS:
        here = [r for r in records if low <= certainty_of(r) < high]
        if not here:
            continue
        said = statistics.mean(certainty_of(r) for r in here)
        happened = accuracy_of(here)
        weighted += abs(said - happened) * len(here)
        total += len(here)
        print(f"    {low:.2f} to {high:.2f}  {len(here):>10,}  {said:>5.1%}  "
              f"{happened:>7.1%}  {happened - said:>+6.1%}")
    if total:
        print(f"    average gap, weighted by how many landed in each band: {weighted / total:.1%}")


def unanimity(records) -> None:
    """Six goes at the same item. Does disagreeing with itself mean anything?"""
    grouped = by_item(records)
    firm, split = [], []
    votes_right, single_right = [], []
    for rows in grouped.values():
        labels = Counter(r["label"] for r in rows)
        (top, count), = labels.most_common(1)
        (firm if count == len(rows) else split).append(accuracy_of(rows))
        votes_right.append(1.0 if top == rows[0]["gold"] else 0.0)
        single_right.append(1.0 if rows[0]["label"] == rows[0]["gold"] else 0.0)
    print(f"  the {len(firm):,} completions it answered the same way {COPIES} times out of "
          f"{COPIES}: {statistics.mean(firm):.1%} agreement")
    if split:
        print(f"  the {len(split):,} it did not: {statistics.mean(split):.1%}")
    print(f"  majority of {COPIES} {statistics.mean(votes_right):.1%} against a single ask "
          f"{statistics.mean(single_right):.1%}")


def against_the_humans(records) -> None:
    """
    The eight point gap, split by whether the humans agreed with each other.

    A judge's mistakes on items two annotators could not agree about are a
    different thing from mistakes on items they both found obvious, and the
    headline number cannot tell them apart.
    """
    if all(r.get("agreed") is None for r in records):
        print("  this corpus carries one label an item and cannot say whether it was "
              "a hard one, so there is nothing to split here.")
        return
    easy = [r for r in records if r["agreed"]]
    hard = [r for r in records if not r["agreed"]]
    print(f"  where both annotators agreed ({len(set(r['source'] for r in easy)):,} completions): "
          f"{item_accuracy(easy):.1%}")
    if hard:
        print(f"  where they did not ({len(set(r['source'] for r in hard)):,} completions): "
              f"{item_accuracy(hard):.1%}")
        share = sum(1 - accuracy_of(rows) for rows in by_item(hard).values())
        everything = sum(1 - accuracy_of(rows) for rows in by_item(records).values())
        print(f"  those {len(set(r['source'] for r in hard)):,} carry "
              f"{share / everything:.1%} of all the disagreement")


def position(records) -> None:
    buckets: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        if record["packed"] > 1:
            buckets[(record["position"] - 1) // 8].append(record)
    if not buckets:
        return
    print("  by where the item sat in its request:")
    for bucket in sorted(buckets):
        here = buckets[bucket]
        print(f"    items {bucket * 8 + 1:>2}-{bucket * 8 + 8:<2}  {accuracy_of(here):>6.1%}  "
              f"({len(here):,} judgements)")


def held_out(records, quiet: bool = False) -> list[tuple[float, float, float, float]]:
    """
    The cut chosen on one half of the items, measured on the other.

    Picking a threshold on the same judgements you then score is how a real
    finding turns into an overfitted one. Items are split rather than judgements,
    because six goes at one item are not independent.

    Returns a row per coverage: what was aimed at, the cut that hit it on half A,
    what share of half B that cut kept, and the agreement over what it kept.
    """
    items = sorted({r["source"] for r in records})
    shaker = random.Random(29)
    shaker.shuffle(items)
    half = set(items[:len(items) // 2])
    fit = [r for r in records if r["source"] in half]
    test = [r for r in records if r["source"] not in half]
    ranked = sorted(fit, key=certainty_of, reverse=True)
    rows = []
    for coverage in COVERAGES:
        edge = certainty_of(ranked[max(0, int(len(ranked) * coverage) - 1)])
        kept = [r for r in test if certainty_of(r) >= edge]
        if kept:
            rows.append((coverage, edge, len(kept) / len(test), accuracy_of(kept)))
    if not quiet:
        print(f"    {'aimed at':>8}  {'cut found on half A':>19}  {'kept in B':>9}  "
              f"{'agreement in B':>14}")
        for coverage, edge, share, score in rows:
            print(f"    {coverage:>7.0%}  {edge:>19.3f}  {share:>8.0%}  {score:>13.1%}")
    return rows


def gap_of(records) -> float:
    """
    Signed calibration error, weighted by how many judgements land in each band.

    Signed and not absolute, because the direction is the finding: a judge that
    is too sure and one that is not sure enough both have a gap, and only one of
    them will hurt you when you set a threshold on it.
    """
    total = weighted = 0
    for low, high in BUCKETS:
        here = [r for r in records if low <= certainty_of(r) < high]
        if here:
            weighted += (accuracy_of(here) - statistics.mean(certainty_of(r)
                                                             for r in here)) * len(here)
            total += len(here)
    return weighted / total if total else 0.0


def compare(paths: list[Path]) -> None:
    """
    The same analysis over several corpora, side by side.

    The triage result was measured on one corpus, and a threshold that
    generalises across tasks is a different claim from one that works on XSTest.
    This is the table that settles which of the two it is.
    """
    print("\nthe same judge, the same analysis, over each corpus\n")
    header = (f"  {'corpus':<9} {'kind':<7} {'items':>6} {'raw':>7} {'95%':>15} "
              f"{'at 90%':>8} {'at 80%':>8} {'at 70%':>8} {'calib':>7} "
              f"{'firm':>7} {'split':>7}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for path in paths:
        records = read(path)
        head = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        grouped = by_item(records)
        low, high = bootstrap([accuracy_of(rows) for rows in grouped.values()])
        aimed = {round(coverage, 2): score for coverage, _e, _k, score in held_out(records, True)}
        firm, split = [], []
        for rows in grouped.values():
            (_top, count), = Counter(r["label"] for r in rows).most_common(1)
            (firm if count == len(rows) else split).append(accuracy_of(rows))
        print(f"  {head.get('corpus', 'xstest'):<9} {head.get('question', 'choice'):<7} "
              f"{len(grouped):>6,} {item_accuracy(records):>6.1%} "
              f"{low:>7.1%} to {high:<5.1%} "
              f"{aimed.get(0.9, 0):>7.1%} {aimed.get(0.8, 0):>7.1%} {aimed.get(0.7, 0):>7.1%} "
              f"{gap_of(records):>+6.1%} "
              f"{statistics.mean(firm) if firm else 0:>6.1%} "
              f"{statistics.mean(split) if split else 0:>6.1%}")
    print("\n  raw       agreement with the labels over everything")
    print("  at N%     agreement over what a cut aimed at N% coverage kept, the cut having")
    print("            been chosen on the other half of the items and not on these")
    print("  calib     how far what happened sat from the certainty the judge stated.")
    print("            Positive means it was right more often than it claimed; negative")
    print("            means it claimed more than it delivered, which is the one that")
    print("            costs you, because a threshold is set on the claim")
    print("  firm      agreement where all six goes at an item said the same thing")
    print("  split     agreement where they did not\n")


def analyse(path: Path) -> None:
    records = read(path)
    grouped = by_item(records)
    scores = [accuracy_of(rows) for rows in grouped.values()]
    low, high = bootstrap(scores)
    head = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    print(f"\n{len(records):,} judgements over {len(grouped):,} items, from {path.name}")
    print(f"corpus {head.get('corpus', 'xstest')}, a {head.get('question', 'choice')} question: "
          f"{head.get('instructions', '')}")
    print(f"\nagreement with the labels {item_accuracy(records):.1%} "
          f"(95% {low:.1%} to {high:.1%})")
    print("\nwhat the judge's own numbers are worth")
    risk_coverage(records, "certainty")
    risk_coverage(records, "confidence")
    print("\nthe same cut, chosen on data it was not then scored on")
    held_out(records)
    print("\nis the probability telling the truth")
    calibration(records)
    print("\nasking the same item more than once")
    unanimity(records)
    print("\nthe eight point gap, split by whether the humans agreed")
    against_the_humans(records)
    print("")
    position(records)
    print("")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyse", default="", help="a file from an earlier run; costs nothing")
    parser.add_argument("--corpus", default="xstest", choices=sorted(corpora.CORPORA),
                        help="which labelled set to ask about")
    parser.add_argument("--compare", nargs="+", default=None, metavar="FILE",
                        help="several earlier runs, read side by side; costs nothing")
    arguments = parser.parse_args()
    if arguments.compare:
        compare([Path(name) for name in arguments.compare])
        return 0
    analyse(Path(arguments.analyse) if arguments.analyse
            else collect(corpora.CORPORA[arguments.corpus]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
