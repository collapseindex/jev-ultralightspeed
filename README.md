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

**A million decisions in 14.7 minutes for $4.99, and not one request failed.** One request per item,
one at a time, would take about 41 hours and cost three times as much.

**Not for one item at a time.** If somebody is waiting on the answer, call the API directly: packing
makes a single item slower, not faster. This is for a queue.

Not affiliated with TypeSafe; the hedgehog is a parody and belongs to nobody.

## A million items

One question, a million support messages, answers streamed straight to a CSV.

| | |
| --- | --- |
| items | 1,000,000 |
| time | 14.7 minutes |
| throughput | 1,134.8 items/s, flat to within 1 item/s over the last 160,000 |
| requests | 31,400, instead of 1,000,000 |
| tokens | 138 per item |
| cost | **$4.99**, confirmed against the account balance to the cent |
| failures | 0 of 31,400 requests, in this run |
| memory | flat: answers stream out, nothing accumulates |

```bash
TYPESAFE_API_KEY=... python soak.py --items 1000000 --out answers.csv
```

Nothing was retried, no rate limit was hit, and the throughput did not sag over a quarter of an hour
of sustained load. To be exact about what that claim covers: it is one run of 31,400 requests, not
an average over repeated runs, and a later 50,000-item run reported the same, 1,570 requests and 0
retried. The client counts retries in `usage.retries`, so anyone can check their own. The comparison in the first line, 41 hours and $15.70, is arithmetic on the
measured sequential rate further down (6.8 items/s, 375 tokens an item), not a run anybody sat
through.

That run is a throughput and stability test: the messages are generated, so there are no human
labels in it. What it can say about answers is that they agreed with the template each message was
generated from **99.79%** of the time, and that none of the 2,091 disagreements came with a
probability above 0.9, and that they were almost all one template: *"We were charged N times for the {plan} plan this morning"*, which Jev
often read as billing to sort out rather than something needing a person today. That is a fair
reading, and it is my label that is arguable. For accuracy against labels a human wrote, see the
next section.

## Same accuracy: measured on a real eval

1,347 completions from [XSTest](https://github.com/paul-rottger/exaggerated-safety), labelled
compliance, refusal or partial by two human annotators, as packaged in
[dinostomp's](https://github.com/collapseindex/dinostomp) `xstest-refusal` pod. The judge is Jev,
asked the same three-way question every time. Only the shape of the requests changes.

The baseline here is not a slow loop: it is one item per request with the same eight requests in
flight, so the 18x is against a client that is already parallel. Against an actual sequential loop
it is far larger, and far less interesting.

| | items/s | requests | tokens/item | cost | agreement with the humans | 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| jev, one request per item | 41.1 | 1,347 | 613 | $0.0322 | 89.2% | 87.5 to 90.8 |
| **jev + ultralightspeed, pack 8** | **270.9** | 169 | 396 | $0.0200 | 90.0% | 88.3 to 91.5 |
| **jev + ultralightspeed, pack 32** | **739.2** | 43 | 374 | $0.0188 | 90.6% | 89.0 to 92.1 |

This is not Jev against some other model. It is **jev-1.13.0 against itself**: same model, same
question, same items, same criteria. The only thing that changes is how this client shapes the
requests.

The three intervals overlap, so the judge is as good packed as it is one item at a time.

One thing worth knowing rather than discovering later. Packing changes which individual items get
which label, even though the total does not move: about 4% of verdicts differ between a packed run
and an unpacked one, against 0.3% between two runs of the same shape. So pack freely when you want
the aggregate, and keep the pack size fixed when you are comparing item by item across runs.

So: 18x the throughput of an already parallel baseline, 41% less money, and the score against the
human labels does not move.

## Measured on synthetic items

256 support messages, one yes-or-no question each, three rounds per shape, shapes run in a random
order, connections warmed first. `agreement` compares every answer with the one-at-a-time baseline.

| shape | items/s | requests | tokens/item | p50 | p95 | $ per 1k items | agreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| sequential | 6.8 | 256 | 375 | 140 ms | 218 ms | 0.0157 | baseline |
| concurrent x8 | 42.3 | 256 | 375 | 181 ms | 284 ms | 0.0157 | 100.0% same |
| concurrent x16 | 78.9 | 256 | 375 | 196 ms | 291 ms | 0.0157 | 100.0% same |
| packed 4 x4 | 99.0 | 64 | 189 | 150 ms | 234 ms | 0.0079 | 100.0% same |
| packed 8 x4 | 182.1 | 32 | 156 | 152 ms | 233 ms | 0.0066 | 99.6% same |
| packed 8 x8 | 338.4 | 32 | 156 | 169 ms | 286 ms | 0.0066 | 99.9% same |
| packed 16 x8 | 527.4 | 16 | 141 | 180 ms | 302 ms | 0.0059 | 99.6% same |
| **packed 32 x8** | **896.5** | 8 | 134 | 239 ms | 281 ms | 0.0056 | 99.2% same |

Short items make packing look even better, because the per-item text is a smaller share of each
request. Reproduce it on your own key, it costs about five cents:

```bash
TYPESAFE_API_KEY=... python bench.py --items 256 --rounds 3
TYPESAFE_API_KEY=... python bench.py --grid              # sweep pack x concurrency
TYPESAFE_API_KEY=... python bench.py --transport threads # without httpx, for comparison
```

Throughput buys latency: a packed request of 32 items takes 239 ms at the median against 140 ms for
one, so the last row is for a queue of a million rows, not for something a person is waiting on. If
you are classifying one item as it arrives, do not use this at all: call the API.

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
| `pack` | 8 | items per request. Higher is faster and cheaper, and slower per request. |
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
