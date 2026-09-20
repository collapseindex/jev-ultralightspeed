# jev-ultralightspeed

**v0.1.0** · Apache-2.0 · no dependencies

Classify a pile of items with [TypeSafe's Jev](https://typesafe.ai) at **47x** the throughput of the
obvious loop, for **64% fewer tokens**, with the same verdicts.

```python
from jev_ultralightspeed import classify

answers = classify(messages, "Does this message need a human today?")
urgent = [a.item for a in answers if a.yes]
```

## Measured

256 support messages, one yes-or-no question each, three rounds per shape, shapes run in a random
order, connections warmed first. `agreement` compares every answer with the one-at-a-time baseline.

| shape | items/s | requests | tokens/item | p50 | p95 | $ per 1k items | agreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| sequential | 6.8 | 256 | 375 | 144 ms | 213 ms | 0.0157 | baseline |
| concurrent x8 | 49.0 | 256 | 375 | 144 ms | 241 ms | 0.0157 | 100.0% same |
| concurrent x16 | 78.6 | 256 | 375 | 157 ms | 464 ms | 0.0157 | 100.0% same |
| packed 4 x4 | 95.7 | 64 | 189 | 145 ms | 332 ms | 0.0079 | 100.0% same |
| packed 8 x4 | 150.9 | 32 | 156 | 175 ms | 418 ms | 0.0066 | 99.9% same |
| packed 8 x8 | 221.1 | 32 | 156 | 165 ms | 589 ms | 0.0066 | 99.5% same |
| packed 16 x8 | 269.9 | 16 | 141 | 345 ms | 688 ms | 0.0059 | 99.7% same |
| **packed 32 x8** | **322.7** | 8 | 134 | 541 ms | 787 ms | 0.0056 | 99.2% same |

Reproduce it on your own key, it costs about five cents:

```bash
TYPESAFE_API_KEY=... python bench.py --items 256 --rounds 3
TYPESAFE_API_KEY=... python bench.py --grid          # sweep pack x concurrency
```

Read the table honestly. Throughput buys latency: a packed request of 32 items takes 541 ms at the
median against 144 ms for a single item, so the last row is for a queue of a million rows, not for
something a person is waiting on. And packing is not perfectly free: about 1 item in 125 changed its
verdict at pack 32, with probabilities moving 0.02 on average. If that matters for your use, stay at
pack 8, which held 99.5% and is still 33x.

## How

Nothing clever. Four things the obvious loop does not do:

1. **Pack.** Several items go in one request as `item_1..item_N`, each with its own question that
   names the item it judges. One round trip covers thirty-two items, and the shared overhead is paid
   once instead of thirty-two times. This is where both the speed and the token saving come from.
2. **Parallel.** Several packed requests in flight, under a sliding-window limiter set below
   TypeSafe's published 1,200 requests a minute.
3. **Keep the connection.** One TLS handshake per worker thread, not per request. On its own this
   nearly doubled the sequential row.
4. **Never ask twice.** Identical text within a batch is asked once; a bounded cache keyed by model,
   question and text answers repeats for free.

## Install

```bash
pip install jev-ultralightspeed        # or: pip install -e .
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
| `requests_per_minute` | 1000 | the ceiling the limiter holds, under TypeSafe's 1,200. |
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
- **It is not an eval harness.** For measuring whether a question is any good, see
  [dinostomp](https://github.com/collapseindex/dinostomp), and for writing the question in the first
  place, [jev-builder](https://github.com/collapseindex/jev-builder).

## Development

```bash
pip install pytest
python -m pytest tests -q        # 18 tests, no network, no key needed
```

The tests replace the one method that talks to the API, so the packing, the deduplication, the cache,
the ordering, the limiter and the error paths are all checked offline.

## License

[Apache-2.0](LICENSE). Not affiliated with TypeSafe.
