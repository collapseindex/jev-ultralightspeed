# jev-ultralightspeed

**v0.1.0** · Apache-2.0 · no required dependencies

<img src="docs/infographic.png" alt="18x faster, 41% cheaper, same accuracy: 739 items a second against 41, and a million decisions in 14.7 minutes for $4.99" width="100%" />

**You have a pile of text and one question about each.** Fifty thousand support tickets to triage.
A quarter of reviews to sort by sentiment. A month of logs to flag. A column to backfill on a table
you already have. This answers the question for all of them, with
[TypeSafe's Jev](https://typesafe.ai), in the time it takes to get a coffee.

```python
from jev_ultralightspeed import classify

answers = classify(tickets, "Does this message need a human to act on it today?")
urgent = [a.item for a in answers if a.yes]
```

**15.9x the throughput of one request per item, 41% less money, and the same accuracy.** Measured
over 30,000 judgements against human labels, in one run, with a script in this repository.

**Not for one item at a time.** If somebody is waiting on the answer, call the API directly: packing
makes a single item slower, not faster. This is for a queue.

Not affiliated with TypeSafe; the hedgehog is a parody and belongs to nobody.

## The benchmark

30,000 judgements over the 1,347 completions in
[dinostomp's](https://github.com/collapseindex/dinostomp) `xstest-refusal` pod, labelled
compliance, refusal or partial by two human annotators, each completion seen about 22 times. Two
arms, identical but for the shape of the requests. This is not Jev against another model: it is
**jev-1.13.0 against itself**, same question, same items, same criteria.

| | regular jev | jev + ultralightspeed |
| --- | ---: | ---: |
| throughput | 41.1 items/s | **653.4 items/s** |
| wall clock | 12 min 11 s | **46 seconds** |
| requests | 30,000 | 942 |
| cost | $0.729 | **$0.430** |
| agreement with the human labels | 89.3% (89.0 to 89.7) | **89.2% (88.9 to 89.6)** |
| same answer across an item's repeats | 99.6% | 98.0% |
| failed | 0 | 0 |
| retried | 1 | 63 |

**15.9x the throughput, 41% less money, and the accuracy is the same**: 89.3% against 89.2%, with
30,000 judgements behind each figure and the intervals sitting on top of each other.

```bash
pip install "jev-ultralightspeed[fast]"
git clone https://github.com/collapseindex/dinostomp.git ../dinostomp
TYPESAFE_API_KEY=... python bench_eval.py            # about 13 minutes, about $1.20
```

Three things in that table are worth reading twice.

**The baseline is not slow.** It is one item per request with the same eight requests in flight, so
the 15.9x is against a client that is already parallel. Against an actual sequential loop it is far
larger and far less interesting.

**Sixty-three retries, and nothing failed.** Packing 32 deep with 8 in flight does hit transient
limits at volume. The client backs off and sends them again, which is why this arm came in at 653
items/s rather than the 800 it reaches on short bursts. Retries are counted in `usage.retries`, so
this is a number rather than an absence of complaints.

**Aggregate accuracy is identical; individual answers are slightly less repeatable.** Ask about the
same completion 22 times and the unpacked client gives the same label 99.6% of the time, the packed
one 98.0%. So pack freely when you want the total, and keep the pack size fixed when you are
comparing item by item across runs.

Shorter items do better than this on every axis, because the per-item text is a smaller share of
each request: a million synthetic support messages (138 tokens each) ran at 1,135 items/s, 14.7
minutes and $4.99 for the lot, over 31,400 requests with nothing retried. `soak.py --items 1000000`
reproduces that, and it is a different corpus, so it is a footnote here rather than a headline.

## How

Nothing clever. Four things the obvious loop does not do, in order of how much they gave:

1. **Pack.** Several items go in one request as `item_1..item_N`, each with its own question that
   names the item it judges. One round trip covers thirty-two items, and the shared overhead is paid
   once instead of thirty-two times. This is where both the speed and the token saving come from.
2. **Parallel.** Several packed requests in flight, under a sliding-window limiter set below
   TypeSafe's published 1,200 requests a minute.
3. **One connection, kept open, multiplexed.** With httpx and h2 installed, every request in flight
   shares a single HTTP/2 connection on one event loop, which measured about twice a thread per
   connection on HTTP/1.1; keeping it open between calls rather than rebuilding it per batch was
   worth another 2.5x. The transport idea is lifted from
   [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast), who got there first.
4. **Never ask twice.** Identical text within a batch is asked once; a bounded cache keyed by model,
   question and text answers repeats for free.

## Install

```bash
pip install "jev-ultralightspeed[fast]"     # httpx and h2: about twice as fast
pip install jev-ultralightspeed             # standard library only, still works
export TYPESAFE_API_KEY=...
```

## Use

```python
from jev_ultralightspeed import Client

client = Client(pack=8, workers=8)     # the defaults are pack=8, workers=4
client.warm()                          # open the connections before the work arrives

answers = client.classify(
    messages,
    "Does this message need a human to act on it today?",
    criteria={"true": "something is broken or costing money right now",
              "false": "a question or a thank-you that can wait"},
    on_progress=lambda done, total: print(f"{done}/{total}", end="\r"),
)

for answer in answers:
    print(answer.label, round(answer.p, 2), answer.item[:60])

print(client.usage)     # 256 items in 1.16s (221.1/s, 32 requests, 156 tokens/item, $0.00166)
```

Pick-one questions work the same way:

```python
answers = classify(tickets, "Which team should handle this?", options={
    "billing": "payments, invoices, refunds",
    "technical": "errors, outages, integrations",
    "account": "logins, passwords, security",
})
```

Every answer carries `item`, `label`, `p`, `distribution`, `confidence` and `kind`, and comes back in
the order you passed the items in, however the requests were shuffled to get there.

### Knobs

| argument | default | what it does |
| --- | --- | --- |
| `pack` | 8 | items per request. Higher is faster and cheaper, and slower per request. On short items: pack 8 does 320 items/s, pack 32 does 1,130. |
| `workers` | 4 | requests in flight. |
| `transport` | `auto` | `http2` when httpx is installed, otherwise `threads`. |
| `requests_per_minute` | 1000 | the ceiling the limiter holds, under TypeSafe's published 1,200. |
| `cache` | True | answer repeats from memory, keyed by model, question and text. |
| `model` | `jev-latest` | passed straight through. |
| `url` | the Jev endpoint | point it at a gateway or a mock. |

## What it does not do

- **It does not change your question.** The only difference between a packed question and a single
  one is the sentence naming which item to judge. There is no bitstring trick and no compressed
  output format, because Jev returns a structured probability per question rather than generated
  text: the output is already about twenty tokens per request.
- **It does not cache across processes.** The cache lives in the client, in memory, bounded at
  10,000 entries.
- **It does not hide failures.** Retries cover 429, 500, 502, 503, 504 and 529 with backoff; anything
  else is raised with what the API said.
- **It is not an eval harness.** It makes a judge fast, not trustworthy. See Related below.

## Development

```bash
pip install pytest
python -m pytest tests -q        # 21 tests, no network, no key needed

TYPESAFE_API_KEY=... python bench_eval.py            # the table above, ~13 min, ~$1.20
TYPESAFE_API_KEY=... python bench.py --items 256     # pack and concurrency sweep, ~5 cents
TYPESAFE_API_KEY=... python soak.py --items 100000   # sustained load, ~50 cents
```

The tests replace the one method that talks to the API, so the packing, the deduplication, the cache,
the ordering, the limiter and the error paths are all checked offline.

## Related

Three tools, one workflow, all Apache-2.0:

Costs are input tokens at TypeSafe's published $0.042 per million for jev-1.13.0, which is the only
rate they list; output tokens are counted but not priced. Every dollar figure here was checked
against the account balance after the run.

- **[dinostomp](https://github.com/collapseindex/dinostomp)** is the harness the benchmark above was
  measured against: pods of labelled items, pre-registered thresholds, a checks registry and a
  findings ledger. It is where you go when the question is whether a judge is any good, not how
  fast it runs. The 1,347 labelled completions in the table are one of its audit pods.
- **[jev-builder](https://collapseindex.github.io/jev-builder/)** writes the request in the first
  place: paste your text, describe the question, and get something you can paste here.
- **jev-ultralightspeed**, this repository, is for when the question already works and there are a
  million rows waiting.

If any of it saves you an afternoon, [sponsorship](https://github.com/sponsors/collapseindex) keeps
it maintained. Not required, and nothing here is gated.

## Security

The key comes from your environment, goes to one endpoint and is never logged, printed, put in an
exception or written to disk. What the library sends, what it keeps in memory, and what it does not
protect you from: [SECURITY.md](SECURITY.md).

## Contributing

Issues and pull requests are welcome. The rules that matter: no required dependencies, never log the
key, and a performance claim needs a measurement rather than an opinion about how HTTP works. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE). Not affiliated with TypeSafe.
