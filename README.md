# jev-ultralightspeed

[![tests](https://github.com/collapseindex/jev-ultralightspeed/actions/workflows/test.yml/badge.svg)](https://github.com/collapseindex/jev-ultralightspeed/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/jev-ultralightspeed)](https://pypi.org/project/jev-ultralightspeed/)
[![Python](https://img.shields.io/pypi/pyversions/jev-ultralightspeed)](https://pypi.org/project/jev-ultralightspeed/)

**v0.24.0** · Apache-2.0 · no required dependencies

<img src="docs/infographic.png" alt="Regular Jev against Jev with ultralightspeed: many more items a second for less money, with the same agreement against human labels" width="100%" />

**You have a pile of text and one question about each.** Fifty thousand support tickets to triage.
A quarter of reviews to sort by sentiment. A month of logs to flag. A column to backfill on a table
you already have. This answers the question for all of them, with
[TypeSafe's Jev](https://typesafe.ai), in the time it takes to get a coffee.

```python
from jev_ultralightspeed import classify

answers = classify(tickets, "Does this message need a human to act on it today?")
urgent = [a.item for a in answers if a.yes]
```

**32x the throughput of one request per item, for 41% less money, with no accuracy difference this
benchmark can detect.** The 32 is the pack depth, and under a ceiling counted in requests that is
arithmetic rather than a measurement. The 41% and the accuracy are measured, over 30,000 judgements
against human labels, with a script in this repository.

**A million short messages: half an hour and $4.99. One request per item: seventeen hours and
$14.97.** Same ceiling, same model, same question. The time is the part you feel.

Those two prices are a [different corpus](#the-benchmark) from the accuracy above: a million
synthetic support messages at 138 tokens each, where the per-item text is a small share of a request
and packing therefore saves most. The benchmark's own items are longer and it saves 41%, which works
out nearer $14 a million packed against $24 unpacked. Length is what moves this number, so measure it
on your own rows before you budget against either one.

**The 32x is a bet on how you are charged, and that is worth saying out loud.** It holds because the
ceiling counts requests. The day it counts tokens instead, the 32 evaporates and the 41% is what is
left. The result that does not depend on the rate card is
[`guidance="once"`](#carrying-the-question-once), which stops repeating the question under every item
and took a live 32-item request from **4,598 billed tokens to 1,777**. It is off by default because
it costs about 0.2 points of agreement, so it is yours to turn on, but it is the saving that survives
the pricing being rewritten.

▶ **[Watch it run](https://github.com/collapseindex/jev-ultralightspeed/raw/main/docs/demo.mp4)**
(sound on), or run it yourself, which takes longer to install than to watch:

```bash
python demo.py                 # both arms, no key needed
python demo.py --speed 1       # in real time rather than at 4x
python demo.py --items 4000    # a shorter one
```

Two counters against the same rate limit, running until the packed arm has finished the job:
**30,000 judgements in 56 seconds, against 938** for the other one. The same 32 as everything else
here, arriving where you can watch it. The replay runs at four times the wall clock, so watching it
takes fifteen seconds, and every number on screen is still the job's own time.

**Not for one item at a time.** If somebody is waiting on the answer, call the API directly: packing
makes a single item slower, not faster. This is for a queue.

Not affiliated with TypeSafe; the hedgehog is a parody and belongs to nobody.

## Is this for your job

Three things decide it: the work is a **queue** rather than a request, the question is a **judgement**
rather than a piece of writing, and the items are **independent** of each other. Everything else is
detail.

| the work | fits | why, and what to set |
| --- | --- | --- |
| grading model outputs against a rubric | **yes** | the measured case. Short items pack deepest, and `triage` matters most where the results feed a decision |
| labelling a dataset before humans see it | **yes** | `triage(keep=0.8)` sends the fifth it is least sure about to a person and keeps the rest |
| triaging tickets, messages, logs | **yes** | short items, so the deepest packs and the cheapest per row |
| re-running a taxonomy change over history | **yes** | this is what `checkpoint` is for: a million rows, and it survives being killed |
| routing or risk-flagging pull requests | **partly** | "does this touch auth", "which reviewer", "is this risky" all work. Writing the review does not |
| classifying documents | **partly** | works, but length costs depth: see the table below |
| moderating hostile user text | **careful** | use `pack=1`. Packing puts thirty-two strangers in one context, see [SECURITY.md](SECURITY.md) |
| summarising, rewriting, extracting fields | **no** | Jev returns a typed answer, not text. There is nothing here to make fast |
| one item with somebody waiting | **no** | packing makes a single item slower. Call the API directly |
| fewer than a few thousand items | **no** | 971 items is thirty packed requests. You do not need a library for that |

**Length is the thing that decides your throughput**, because a request holds about 98,000 characters
of items and depth is what one unit of the rate limit buys:

| what it is | characters | packs | items/s | a million takes |
| --- | ---: | ---: | ---: | ---: |
| a support message | 300 | 64 | 1,067 | 16 minutes |
| a model completion | 1,100 | 64 | 1,067 | 16 minutes |
| a PR title and diff stat | 2,000 | 49 | 817 | 20 minutes |
| a page of a document | 5,000 | 19 | 317 | 53 minutes |
| a long email thread | 10,000 | 9 | 150 | 1.9 hours |
| a contract clause set | 20,000 | 4 | 67 | 4.2 hours |

Depth is capped at 64 in that table because that is as deep as this has been measured, not because the
format stops. A single item is refused over 20,000 characters. If your rows are long, read
[Trimming the items](#trimming-the-items) before cutting them down, because below a point that you have
to measure it does not degrade gracefully, it falls over.

## The benchmark

30,000 judgements over the 1,347 completions in
[dinostomp's](https://github.com/collapseindex/dinostomp) `xstest-refusal` pod, labelled
compliance, refusal or partial by two human annotators, each completion seen about 22 times. Two
arms, identical but for the shape of the requests, both holding under TypeSafe's published ceiling
of 1,200 requests a minute. This is not Jev against another model: it is **jev-1.13.0 against
itself**, same question, same items, same criteria, same machine.

| | regular jev | jev + ultralightspeed |
| --- | ---: | ---: |
| items on one unit of the ceiling | 1 | **32** |
| items/s at 1,000 requests a minute | 16.7 | **533** |
| requests for 30,000 judgements | 30,000 | 940 |
| cost | $0.729 | **$0.430** |
| agreement with the human labels | 89.2% (87.5 to 90.8) | 89.2% (87.6 to 90.8) |
| same answer across an item's repeats | 99.6% | 98.1% |
| failed | 0 | 0 |
| retried | 1 | 0 |

**The throughput row is arithmetic and that is the point.** A ceiling counted in requests does not
care what a request contains, so 32 items on one beats 1 item on one by 32, and no run can do better
than that without borrowing from somebody. The unpacked arm proves the model: 30,000 requests in
1,801 seconds is 999 a minute against a ceiling of 1,000, and 16.67 items a second to three figures.

**Two earlier numbers here were that 32 bent by something else, and both are withdrawn.** The first
said 26.5x, because the arms ran one after the other and the packed arm went second, after half an
hour of load: it took **245 retries against the other's 1**, and the waiting cost it 17% of its rate.
Running them in alternating turns instead, the retries went to **zero** and the shortfall with them,
which settles what those 245 were. The second said 43.9x, from that same rerun, because sharing one
ceiling let the packed arm use the headroom the other was not touching while it waited its turn: it
ran at 1,372 requests a minute, and 730 items a second is not a rate anything can sustain. Alone at
the ceiling it does 532, which is the arithmetic again.

**The accuracy is the measured part, and it got tighter.** Over the 1,347 completions, packed minus
one-per-request is **+0.01 points, 95% interval −0.72 to +0.75**, against −0.09 and −0.83 to +0.61
before, with both arms landing on 89.2%. Inside a two point margin either way, which is "no
difference worth caring about at this sample size" and not proof of equivalence.

**89% is not 89% of a perfect score.** The two annotators who labelled this pod agreed with each
other on 1,310 of the 1,347 completions, so the ceiling is **97.3%**, not 100%, and the remaining
37 have a consensus label that one of the two humans disagreed with. Read both arms against that.

```bash
pip install "jev-ultralightspeed[fast]"
git clone https://github.com/collapseindex/jev-ultralightspeed.git
git clone https://github.com/collapseindex/dinostomp.git
cd jev-ultralightspeed
TYPESAFE_API_KEY=... python bench/eval.py            # about 35 minutes, about $1.20
```

### Why the baseline is slow, and why that is the point

**The ceiling counts requests, not items.** TypeSafe publishes 1,200 requests a minute, so a client
sending one request per item cannot exceed about 20 items a second however many threads it runs.
That is arithmetic, not a slow client: the baseline here sits at 16.7 items/s because the limiter
holds it under the ceiling, and no well-behaved client can do better one item at a time.

Packing is the only way past it. Thirty-two items in one request spends one unit of the budget
instead of thirty-two, which is why the packed arm reaches 533 items/s while making a thirtieth of
the requests.

An earlier version of this table reported the baseline at 41 items/s, about 2,460 requests a
minute. That was this library failing to apply its own rate limit on the fast path: the number was
real but a well-behaved client cannot reproduce it. The bug is fixed, the old figure is in the
changelog, and the honest comparison is the one above.

### Where an item sits in the request

An aggregate hides a position effect that cancels out, so `bench/packing.py` asks directly. 10,776
judgements, packed 32 deep, each completion seen 8 times, run twice: once over a shuffled queue and
once over a queue sorted so that each pack is full of near-identical items, the way a real queue
ordered by topic or customer would be.

| | shuffled queue | sorted queue |
| --- | ---: | ---: |
| agreement with the human labels | 89.3% | 89.4% |
| same answer across an item's repeats | 98.1% | 99.6% |
| items 1-8 | 90.2% | 90.7% |
| items 9-16 | 89.1% | 89.6% |
| items 17-24 | 88.5% | 90.1% |
| items 25-32 | 89.5% | 87.4% |
| spread across positions | 1.7 points | 3.4 points |

Two things worth taking from it. A sorted queue is not worse in aggregate, and its answers are
actually steadier, presumably because a pack of similar items is a more consistent context. But the
position effect doubles: in the sorted arm the last eight items scored 3.3 points below the first
eight. With about 1,347 independent completions behind each arm, a spread of a point or two is near
the edge of what this run can separate from noise; three is harder to dismiss.

What to do with that: **shuffle before packing** if your queue arrives sorted, and treat a deep pack
as approximate per item, exact in aggregate. Every answer carries `position` and `packed`, so you
can check this on your own data.

### Three more things in that table

**The intervals are clustered, not binomial.** The 30,000 judgements are 1,347 completions seen 22
times each, and the repeats are not independent: this run measures them agreeing with themselves 98
to 99.6% of the time. Treating them as 30,000 independent draws would report an interval about five
times narrower than the evidence supports. `bench/eval.py` computes each completion's own accuracy
and bootstraps over the completions.

**The 245 retries were an artifact of going second, and they are gone.** The first version of this
benchmark ran the arms one after the other, unpacked first. That arm needs 30,000 requests, so it held
the floor for half an hour before the packed arm started, and the packed arm then took 245 retries
against its 1. Two explanations were floated here at various points, that a token-counted limit was
binding and that the service was simply busy, and both were guesses. Running the arms in alternating
turns instead answered it: **zero retries**, and the 17% rate shortfall went with them.

Which also thins out the token-limit story that replaced it. At $0.430 of input in 68 seconds the
packed arm was moving about 9M input tokens a minute against a documented 250,000 a second, or 15M a
minute, so it was never near that ceiling and never needed to be.

What survives is the part that does not depend on anyone's rate tier at all: **41% fewer tokens, so
about 1.7x** on a token-counted allocation rather than a request-counted one. Treat the 32x as what a
request ceiling buys you and the 1.7x as what a token ceiling does.

**How much room is left: none worth chasing, and the run proves the model.** The unpacked arm sent
30,000 requests in 1,801 seconds, which is **999 a minute against a ceiling of 1,000**, and 16.67 items
a second to three figures. The client reaches its configured ceiling and the ceiling is the wall. At
`pack=32` that wall is 533 items/s, and getting past it needs a higher allocation, fewer tokens an
item, or fewer items, not a faster client.

A slow run can now say why it was slow, which is how the 245 were finally settled. `usage.pushback`
counts retries by status code and `usage.waited` totals the time spent sitting them out:

    8 items in 1.00s (8.0/s, 8 requests, 3 retried (2x429, 1x503, 0.6s waiting), ...)

| pack | items/s ceiling at 1,000 requests a minute |
| ---: | ---: |
| 8 | 133 |
| 32 | 533 |
| 64 | 1,067 |

**Four requests in flight gets to within 1% of that ceiling, in a burst.** This was worth measuring
because arithmetic said otherwise: dividing the headline run's 68 seconds by its 942 requests and 8
workers gives 577ms, and 4 workers at 577ms would allow only 416 requests a minute. That reasoning is
wrong, because 577ms is wall clock over workers and folds in every retry wait rather than being the
latency of a request. Measured at `pack=32` over 40 requests an arm:

| workers | p50 | mean | requests/min | of the 1,000 ceiling |
| ---: | ---: | ---: | ---: | ---: |
| **4** | 202ms | **242ms** | **993** | **99.3%** |
| 8 | 270ms | 318ms | 1,510 | 151% |
| 12 | 327ms | 357ms | 2,017 | 202% |
| 16 | 337ms | 386ms | 2,485 | 249% |
| 24 | 480ms | 550ms | 2,616 | 262% |

So the default of four is not holding anything back, but it has no margin either: it reaches 99.3% of
the ceiling with nothing spare, and latency is what decides that. More workers buy burst rather than
sustained throughput, since sustained is whatever the limiter allows and nothing else, and they are
not free: mean latency more than doubles between 4 and 24, so the service queues and the arithmetic
saying twenty-four workers are six times four is out by about 60%.

**Two things that table cannot tell you.** Forty requests cannot fill a sixty second window, so every
figure in it is a burst and none is a sustained rate. And the mean is what governs a queue's
throughput, not the median, which is why the mean is the column to read. An earlier version of this
section quoted a rate derived from the median and was optimistic by about a fifth, as well as
circular, since in a burst the measured rate is the latency bound. Raise `workers` if you raise
`requests_per_minute`, or if you see the request rate falling short. Otherwise leave it.
`bench/workers.py` reruns this for about ten cents.

#### The burst, held for ten minutes

The first of those was untested for four releases, and it was the one that mattered: two throughput
claims have been withdrawn from this README already and both were short runs read as rates. So here
is a long one. Eight workers, `pack=32`, the default ceiling of 1,000 a minute, ten minutes without
stopping, 320,000 items for $1.61:

| minute | requests/min | items/s | retries |
| ---: | ---: | ---: | ---: |
| 1st | 999 | 533 | 0 |
| 2nd to 9th | 996 to 1,003 | 531 to 535 | 0 |

**1,000 a minute, flat, for the whole run.** Front half against back half is a drift of +0.0%, so the
rate is a rate and not a bucket draining slowly. 533 items a second is the ceiling exactly: at
`pack=32` there is nothing left on the table, and the reason the earlier numbers looked like a burst
is that they were measured before the limiter had anything to do.

What a short run actually looks like is worth seeing, because it is why those claims were withdrawn.
In the first 90 second trial the request count climbed to 999 by t=27s, then sat perfectly still
until t=61s when the window rolled. Every sub-minute measurement in this repository was reading that
first slope.

Two more things fell out of it. Tokens moved at 63,700 a second against a published ceiling of
250,000, so **the request ceiling binds first and by four times over**, which is the whole arithmetic
behind packing. And over 13,000 requests against the live service there were no 429s at all and eight
dropped connections, each retried and recovered.

```bash
TYPESAFE_API_KEY=... python bench/sustained.py --minutes 10 --budget 2.50
python bench/sustained.py --analyse data/results/<file>
```

It stops at the clock or the budget, whichever comes first, and the budget is enforced by the
generator feeding it simply stopping, so nothing is cancelled mid-flight and the last requests still
land and count.

Note that this measured eight workers, not the default four. Four reaching the ceiling is still a
burst measurement.

**Whether the limiter itself holds is not measured here**, and the first version of this bench got
that wrong twice in one run: it counted responses, which bunch, and then counted permits with a stamp
taken just after each one rather than at it. Both read one request over the ceiling and neither meant
anything. The ceiling is a property of the limiter and needs no API to test, so it is checked in the
test suite instead, against the limiter's own timestamps, over an hour of simulated traffic offered
at three times the ceiling: never more than 1,000 in any 60 seconds, and a permit exactly 60.000
seconds old is outside the window.

It is also not compute-bound. At 533 items/s and 341 tokens an item it is moving roughly 700 KB/s of
JSON, so `orjson`, `uvloop` and more cores have nothing to do here. Every remaining lever is about
permission: fewer tokens, fewer requests, more ceiling, or not asking at all.

**Carrying the question once, `guidance="once"`.** The question block is repeated once per item
inside a packed request, which is 74.5% of the body at `pack=32`, and bytes per item are flat across
pack depth. Carrying it once in the state instead cut a live 32-item request from **4,598 to 1,777
billed input tokens**, a 61% saving, and it is measured rather than estimated. See below for what it
does to the answers, and why it is not the default.

**The two arms differ by one sentence as well as by shape.** A packed request ends with "Judge
item_N only, ignoring every other item"; an unpacked one has nothing to disambiguate and so does
not carry it. That is a confound, and an honest one to name: the paired interval of −0.83 to +0.61
points is wide enough to absorb a wording effect of that size, but it is not zero.

**Aggregate agreement holds; individual answers are slightly less repeatable.** Ask about the same
completion 22 times and the unpacked client gives the same label 99.6% of the time, the packed one
98.1%. Pack freely when you want the total, and keep the pack size fixed when you are comparing
item by item across runs.

Shorter items do better than this on the cost axis, because the per-item text is a smaller share of
each request: a million synthetic support messages at 138 tokens each cost $4.99 for the lot, about
a third of what one request per item would spend. `bench/soak.py --items 1000000` runs it.

That run's *throughput* figures are not quoted here on purpose. They were measured before the rate
limit reached the fast path, at about 2,136 requests a minute, which no client honouring the
published ceiling can reproduce. Under the limiter the same job is bounded by the same arithmetic as
everything else: 31,400 requests at 1,000 a minute is half an hour, whatever the network does.

## How

Nothing clever. Four things the obvious loop does not do. The first is the headline; the other
three are what stop it becoming the next bottleneck:

1. **Pack.** Several items go in one request as `item_1..item_N`, each with its own question that
   names the item it judges. One round trip covers thirty-two items, the shared overhead is paid
   once instead of thirty-two times, and one unit of the rate limit buys thirty-two judgements
   instead of one. Against a limit counted in requests, this is the entire 32x.
2. **Parallel.** Several packed requests in flight, under a sliding-window limiter set below
   TypeSafe's published 1,200 requests a minute.
3. **One connection, kept open, multiplexed.** With httpx and h2 installed, every request in flight
   shares a single HTTP/2 connection on one event loop. Measured against a thread per connection on
   HTTP/1.1 it was worth about 2x, and keeping the connection between calls another 2.5x, in the
   unlimited regime this library used to run in by mistake. Under the rate limit those gains mostly
   stop showing up in the headline, because the ceiling binds first. They are what keeps a packed
   run from spending its budget on handshakes, and they matter again the moment your limit is
   raised. The transport idea is lifted from
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

client = Client(pack=32, workers=8)    # the defaults are pack=32, workers=4
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

How sure the judge is, `answer.certainty`, is the lever that matters most. Setting aside the fifth it
is least sure about takes agreement with human labels from 89.7% to **96.8%**, which is the
annotators' own agreement rate. See [Knowing which verdicts to trust](#knowing-which-verdicts-to-trust).

For a pick-one question `certainty` is `p`, the chosen option's own probability. For a yes/no question
it is not: `p` is the probability of **yes**, so 0.01 is a confident no. Rank by `certainty` or let
`triage` do it.

### Knobs

| argument | default | what it does |
| --- | --- | --- |
| `pack` | 32 | items per request, and the whole ballgame: it decides how many items one unit of the rate limit buys. Raised from 8 in v0.10.0 on the measurement in [What to set](#what-to-set). Use `pack=1` for adversarial text. |
| `workers` | 4 | requests in flight. Measured to be enough to reach the default ceiling on its own; more buy burst, not sustained throughput, and latency more than doubles between 4 and 24. Raise it if you raise `requests_per_minute`. |
| `transport` | `auto` | `http2` when httpx is installed, otherwise `threads`. |
| `requests_per_minute` | 1000 | the ceiling the limiter holds, under TypeSafe's published 1,200. |
| `paced` | False | spread requests evenly instead of letting a minute's worth go at once. Only useful against a service that throttles short bursts, and see below before turning it on. |
| `chars_per_token` | 3.5 | what the pack planner assumes a token costs. Conservative for English against 3.92 measured, and badly wrong for code, CJK or emoji-heavy text, where a token can be one character. Lower it there. |
| `limiter` | its own | hand several clients one ceiling to share. Anything with `take()` and `try_take()` does, so a window held in Redis across machines drops straight in. This library does not ship one of those, it gets out of the way. |
| `cache` | True | answer repeats from memory, keyed by model, question and text. |
| `dedupe` | True | identical text in one call is asked once. Turn it off when the repeat **is** the measurement: with it on, asking the same item twenty times costs one request and returns twenty copies, which looks like perfect consistency and is not. |
| `model` | `jev-latest` | passed straight through. |
| `url` | the Jev endpoint | point it at a gateway or a mock. Plain `http` is refused unless the host is this machine, because a bearer token over plaintext is a key read by anything on the path, and that happens through a typo rather than a decision. `allow_insecure_http=True` if you meant it. |
| `verify` | the machine's trust store | a CA file or an `ssl.SSLContext`, for a gateway signed by a private CA. Never a boolean: switching verification off is something you should have to write out yourself. A context you pass is modified on the fast path, because ALPN has to advertise h2 on the context itself or the connection quietly comes up as HTTP/1.1 and a context cannot be copied. |
| `guidance` | `repeat` | `once` carries the question in the state instead of in every item's question. Large saving on short items, almost none on long ones, and it changes the prompt. See below. |

`client.http_version` says what the connection actually negotiated, `"HTTP/2"` or `"HTTP/1.1"`, after
the first call. It is worth looking at once. httpx falls back to HTTP/1.1 whenever ALPN does not
offer h2 and says nothing about it, so a proxy that strips ALPN costs you the entire reason for
installing the extra, silently. A test now runs the fast path against a real h2 server and checks
that 16 requests arrived on **one** connection with more than one stream open at a time, which is
the claim this package is built on and was previously taken on faith.

`pack` is a maximum, not a promise. TypeSafe documents 64k tokens in a request and 32k for the state
plus the longest question, and `pack` counts items, so several individually legal items can make one
illegal request. Groups are split to stay well under both limits, estimated at a deliberately
pessimistic 3.5 characters per token against 3.92 measured on a live packed request. Short items are
unaffected and still pack to `pack`.

### Scoring along a list

Jev answers three shapes of question and this client now sends all three. `criteria` makes it yes/no,
`options` makes it pick-one, and **`levels` makes it a score**: an ordered list from low to high,
where the answer lands somewhere along it rather than on one of them.

```python
answers = classify(messages, "How angry is the customer?",
                   levels=["Calm", "Mildly annoyed", "Frustrated", "Angry", "Furious"])

for answer in answers:
    print(answer.score, answer.label)      # 2.53  Angry
```

Live, on four support messages:

| `score` | nearest level | message |
| ---: | --- | --- |
| 0.00 | Calm | thanks so much for sorting that out yesterday |
| **2.53** | Angry | this is the third time I've had to chase this |
| 0.00 | Calm | quick question about my invoice date, no rush |
| 3.99 | Furious | ABSOLUTELY UNACCEPTABLE. I want a refund |

The 2.53 is the point: it sits between "Frustrated" and "Angry" because the probability is split
between them, which a pick-one question cannot express. `answer.score` is that number,
`answer.label` is the nearest level by name, and `answer.distribution` is the spread across all of
them under your own names rather than under `"0"`, `"1"`, `"2"`.

Levels stay in every question rather than moving into shared guidance, for the same reason option
names do: they are the answer space, not wording. And since the API takes all three kinds in one
request, `judge` can mix them:

```python
judge([Ask(row, instructions="Urgent?"),
       Ask(row, instructions="Which team?", options=teams),
       Ask(row, instructions="How angry?", levels=ladder)])     # one request
```

#### What a score is worth, measured against human labels

The `xstest-refusal` pod's three labels happen to be ordered, compliance to partial to refusal, so the
same 1,347 completions carry over honestly as a ladder rather than a set. 5,388 judgements, four
copies each, thresholds chosen on one half of the completions and measured on the other:

**Asking it as a score cost 1.3 points.** 87.9% (86.2 to 89.6) against 89.2% for the same items asked
as a pick-one. The intervals overlap, so this is a lean rather than a result, but it leans the wrong
way and it is worth knowing before reaching for a ladder when a set would do.

**`triage` works on this path too, slightly less precisely.** Aiming at a 97% bar keeps 84% of the
judgements and lands on 95.8%, where the pick-one path kept 81% and landed on 96.8%. The ranking
separates, the threshold transfers less exactly.

**And a score tells you something a pick-one cannot: how far it landed from any level.**

| distance from the nearest level | judgements | agreement |
| --- | ---: | ---: |
| on a level, under 0.05 | 3,658 | **98.5%** |
| 0.05 to 0.15 | 740 | 82.8% |
| 0.15 to 0.30 | 498 | 61.8% |
| stranded between two, 0.30 and up | 492 | **42.9%** |

98.5% against 42.9%. A pick-one question answers "partial" and stops; a score answers 1.47 and tells
you it could not decide between two rungs, which is the same information a human reviewer would want
and it arrives for free. Ranking by that distance instead of by probability keeps 78% at the 97% bar
and lands on 96.5%, so the two signals are worth about the same and they are not the same signal.

**This is a new question shape, not a new corpus.** The score numbers on this page all come from the
one pod and the one task, and this section does not change that. The triage result is the exception
and is measured on [three corpora](#does-it-hold-anywhere-but-xstest); the rest of the accuracy
figures here are still XSTest.

```bash
TYPESAFE_API_KEY=... python bench/score.py      # the table above, about seven cents
```

### When every row asks something different

`classify` sends one question about every item. The API does not work that way: its body carries a
question **per item**, and `classify` is the case where they all happen to be the same. `judge` is the
case where they are not.

```python
from jev_ultralightspeed import Ask, judge

answers = judge(
    [Ask(row.text, options=row.allowed_labels) for row in rows],
    instructions="Which of these applies?",
)
```

This matters most when the answer space itself differs per row, because then there is nothing to group
by and packing has nothing to work with. Sixty-four rows that each pick from their own set of labels,
against a local server:

| | requests |
| --- | ---: |
| `classify`, one call a row | 64 |
| `judge` | **2** |

Which is the pack depth again, and it is the whole of the library's benefit arriving for a workload
that was getting none of it.

Anything an `Ask` leaves out falls back to what the call was given, so mixing is fine. Packing,
deduplication, the cache, the checkpoint and `triage` all behave as they do for `classify`, with two
differences worth knowing: two rows count as duplicates only when the question matches as well as the
text, and `guidance="once"` needs a pack to be asking one thing before there is anything to hoist, so a
mixed pack keeps its questions where they are.

### Trimming the items

Once the question stopped being repeated per item, nearly everything left in the bill is the items. It
is also what caps the pack, since a request is limited by what fits in the state, and depth is what
one unit of the rate limit buys. So a shorter item is cheaper twice over.

`bench/trim.py` cuts the pod's completions to several lengths, both ends, 2,694 judgements an arm,
thresholds chosen on one half of the completions and measured on the other:

| kept | mean chars | tokens/item | agreement | kept at 97% | $ per 1k trusted | pack that would fit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| everything | 1,091 | 384.8 | 89.4% | 80% | 0.018 | 89 |
| first 1000 | 852 | 336.7 | **89.7%** | 80% | 0.015 | 115 |
| **first 500** | 499 | 264.6 | 88.6% | **80%** | **0.012** | **196** |
| last 300 | 316 | 222.0 | 74.8% | 39% | 0.019 | 310 |
| first 300 | 316 | 223.8 | **37.3%** | **0%** | n/a | 310 |
| first 150 | 166 | 193.4 | 57.3% | 0% | n/a | 590 |

**Trimming to 500 characters is worth it here.** It costs 0.8 points of raw agreement, keeps the same
80% coverage at a 97% bar, takes a third off the cost per trusted item, and more than doubles the pack
that fits. That last column is arithmetic from the 28k state budget, not a measured configuration: the
coverage figures were all taken at `pack=32`, and a pack of 196 would need its own measurement.

**Below that it does not degrade, it falls over.** At 300 characters agreement is 37.3%, which is
worse than always answering "compliance" (57.7% of this pod). The failure is not gradual and it is not
monotonic, because 150 characters scores *better* than 300:

| kept | what it answered | mean `p` |
| --- | --- | ---: |
| everything | compliance 63%, refusal 35% | 0.94 |
| first 500 | compliance 59%, refusal 38% | 0.94 |
| first 300 | **refusal 94%** | **0.57** |
| first 150 | compliance 99% | 0.58 |

At 150 characters there is no signal, so it falls back on the commonest answer and lands on the base
rate. At 300 there is a *misleading* signal, because the opening of a completion often reads like the
start of a refusal, and it commits. **A partial input is worse than a tiny one**, which is not what
anyone would guess, and it is the reason to measure a trim rather than pick one.

**The signal is not at the front.** Keeping the last 300 characters scores 74.8% against 37.3% for the
first 300, a 37 point difference for the same number of characters. Which end matters is a property of
your question, so try both.

**Triage caught the cliff.** On the broken arms mean `p` fell from 0.94 to 0.57 and no threshold
reached the 97% bar, so coverage came back as zero: trust none of this. That is a different failure
from the one `triage` was measured on, and it held.

```bash
TYPESAFE_API_KEY=... python bench/trim.py        # six arms, ~15 cents
python bench/trim.py --analyse data/results/<file>
```

### What to set

Tuning for throughput alone stopped being the right objective the moment `triage` existed, because
`triage` buys accuracy with coverage. What a shape is worth is what it delivers **past a quality
bar**:

    trusted items a second = items a second x the share you can keep at the target

`bench/tuning.py` prices eight shapes that way over 21,552 judgements on the `xstest-refusal` pod,
thresholds chosen on one half of the completions and measured on the other, arms run in a random
order. At a 97% bar, which is the annotators' own agreement rate:

| pack | items/s under the ceiling | kept at 97% | **trusted items/s** | $ per 1,000 trusted |
| ---: | ---: | ---: | ---: | ---: |
| 8 | 133 | 75% | 100 | 0.020 |
| 16 | 267 | 78% | 208 | 0.019 |
| **32** | **533** | **77%** | **409** | **0.019** |
| 64 | 1,067 | 79% | 848 | 0.018 |

**Depth is nearly free, and that was the open question.** The worry about packing has always been
that it makes each verdict shakier. If it did, coverage would fall as depth rose and the trusted
column would flatten. It does not: coverage moves from 75% to 79% going from 8 to 64, and raw
agreement moves 0.2 points (89.1% to 88.9%). So the default is `pack=32`, and 64 is there if your
items are short enough to fit it.

Those throughput figures are **what the request ceiling allows**, `pack x 1000/60`, not what the
benchmark clocked. Each arm is short enough that the limiter's sixty second window never fills, so
every one of them burst above its own sustained rate: the `pack=8` arm ran at 2,087 requests a
minute. A long job cannot.

**No position effect is detectable at any depth.** Comparing the front half of a request against the
back half, which is the same two-way comparison whatever the pack depth, the gaps are −1.6, −1.2,
+0.5, +0.2, −1.4, −1.3, +1.5 and +1.3 points across the eight shapes: either side of zero, all within
about one standard error. An earlier version of this section reported a spread that grew with depth,
which was an artifact of comparing the widest gap between four bands against the widest between eight.
More bands means a wider widest gap whatever the data says.

**`guidance="once"` costs a little, consistently.** Raw agreement is lower in all four depths tried,
by 0.2 to 0.5 points, which lines up with the paired −0.20 measured separately. Coverage is
unchanged. Worth it when the question is long relative to the items, not otherwise.

```bash
TYPESAFE_API_KEY=... python bench/tuning.py        # eight shapes, ~35 cents
python bench/tuning.py --analyse data/results/<file>
```

### Pacing, and why it is off

The limiter holds a minute's worth, which means a thousand requests may all leave in the first second
of a minute and nothing after. That is inside TypeSafe's published ceiling and it is not inside every
service's idea of fair. `paced=True` puts a floor under the gap between sends instead:

```python
client = Client(pack=32, workers=8, paced=True)
```

Against a local server that throttles anything over five requests per 200ms, it is the difference
between fighting the throttle and not:

| | items/s | retries | requests attempted |
| --- | ---: | ---: | ---: |
| bursting | 113.4 | 21 | 53 |
| paced | **196.4** | **1** | **33** |

**That is a local server, and we could not make the real one behave like it.** Across the eight arms
of the tuning sweep the client burst to between 915 and **2,087 requests a minute** against a
published 1,200, and at `pack=64` it pushed past the documented 250,000 tokens a second as well.
Retries across all eight: **zero**. So on the public endpoint, today, pacing had nothing to fix, and
turning it on can only make a short job slower.

It was kept partly because the headline benchmark's 245 retries were unexplained. They are explained
now: the packed arm was going second, after half an hour of load, and alternating the turns took them
to zero. So the only argument left for pacing is that somebody on a stricter allocation may meet a
throttle this account does not. If `usage.retries` is climbing, try it. If it is not, do not.

```bash
python bench/sweep.py --throttle --items 256 --rounds 3    # the table above, no key needed
```

### Knowing which verdicts to trust

Throughput is within a fifth of its arithmetic ceiling. Agreement is **eight points** below the human
one, 89.3% against annotators who agreed with each other 97.3% of the time, so that is the bigger gap
by a lot. The useful thing is not that the judge is 89% right. It is that the judge's own probability
says which 89%.

```python
from jev_ultralightspeed import classify, triage

answers = classify(rows, question, options=labels)
trusted, review = triage(answers, keep=0.8)      # or at_least=0.92
```

8,082 judgements over the 1,347 human-labelled completions in dinostomp's `xstest-refusal` pod,
packed 32 deep. The cut is chosen on one half of the completions and measured on the other, because a
threshold picked on the data it is then scored against is not a finding:

| kept | cut at | agreement |
| ---: | ---: | ---: |
| 100% | | 89.7% |
| 89% | 0.770 | 94.4% |
| **81%** | **0.920** | **96.8%** |
| 72% | 0.970 | 98.5% |
| 62% | 0.990 | 99.3% |

**Set aside the fifth it is least sure about and the rest is at the human ceiling.** Those two figures,
96.8% and 97.3%, are not the same measurement on the same set, so read it as "at the ceiling on the
part it is sure about" rather than "as good as a person". What it means in practice is that a million
rows do not need a million pairs of eyes; they need eyes on about two hundred thousand, and the judge
picks which.

#### Does it hold anywhere but XSTest

That was one corpus, one domain, one pair of annotators, which is the weakness in it. A threshold
that generalises across tasks is a different claim from one that works on refusal classification, so
the same run now goes against two more, picked to be unlike it and unlike each other. **BoolQ** is
yes/no reading comprehension over Wikipedia passages, and the only one of the three that exercises
the question type this library is named after. **AG News** is four way topic labelling that needs no
reasoning at all, only recognition. Same judge, same analysis, same held-out split:

| corpus | question | items | agreement | at 90% | at 80% | at 70% | calibration | agrees with itself | wavers |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| xstest | pick one of 3 | 1,347 | 89.3% | 94.4% | **96.8%** | 98.5% | -4.4% | 92.7% | 43.4% |
| boolq | yes/no | 3,270 | 91.0% | 94.0% | **95.3%** | 96.1% | +5.8% | 93.2% | 53.7% |
| ag_news | pick one of 4 | 3,270 | 88.7% | 92.2% | **94.7%** | 95.6% | -6.1% | 91.4% | 52.0% |

**The shape holds on all three.** Setting aside the least-sure fifth is worth 7.1 points on XSTest,
4.3 on BoolQ and 6.0 on AG News, each measured on items the cut was not chosen on. The wavering
signal holds too: where all six goes agree the judge is right 91 to 93% of the time, and where they
do not it is close to a coin flip on every corpus. Asking six times still buys nothing anywhere
either (BoolQ 91.3% voted against 91.0% asked once, AG News 88.8% against 89.0%).

**What does not transfer is the number.** The cut that keeps four fifths is 0.92 on XSTest, 0.77 on
BoolQ and 0.95 on AG News. Carry a threshold across and you will keep half of one corpus and nearly
all of another. The direction of the miscalibration flips as well: on BoolQ the judge is *less* sure
than it turns out to be, on the other two more. So there is no constant to take from this table, only
a method, and the method is one call: [`calibrate`](#finding-your-own-cut) runs it over a few hundred
of your own labelled rows for cents.

One caveat that belongs to easy tasks rather than to the method. On AG News four judgements in five
come back at a probability of 1.000, so the ranking runs out of resolution: no cut keeps fewer than
63% of the items, and the bottom three rows of its risk-coverage table are all that same 63%. Where a
judge is sure of nearly everything there is correspondingly little for triage to sort.

```bash
python bench/corpora.py --build boolq ag_news    # downloads once; the only thing here that goes out
python bench/confidence.py --corpus boolq
python bench/confidence.py --compare data/results/*confidence*.jsonl
```

Four more things fell out of the XSTest run, and two of them are negative results worth as much as
the positive one.

**Asking the same item six times buys nothing.** Majority of six: 89.5%. A single ask: 89.7%. Six
times the tokens for a fifth of a point in the wrong direction. Self-consistency voting is the first
thing anyone reaches for, and here it is a waste.

That one has a reason behind it rather than being a lone number, which is why it is likely to hold on
your data too. Voting removes variance, and there was almost none to remove: the same completion asked
twenty-two times comes back the same way 98.1% of the time packed and 99.6% unpacked. A judge that
already agrees with itself has nothing to average out. LangChain measured the same fact from another
angle, putting Jev's score variance 92 to 913 times below GPT-5.6 and Claude Sonnet 4.6 as judges.
Where voting earns its keep is where a judge is unstable, and this one is not.

**Disagreeing with itself is a signal even though voting is not.** The 1,256 completions it answered
the same way all six times: 92.7%. The 91 where it wavered: 43.4%. So the wavering identifies the hard
ones, it just does not fix them.

**The probability ranks but does not mean what it says.** Against the human labels it runs over in the
middle of its range: where it said 74.8% it was right 51.0% of the time, where it said 92.3% it was
right 78.4%. Weighted across the bands the gap averages 4.4 points. Sort by `p`, do not read it as a
chance of being right. Some of that is definitional, since the model's probability is about its own
answer and not about agreeing with these two annotators, but the practical advice is the same.

**A sixth of the disagreement is on rows the humans could not agree on either.** Where both annotators
agreed, 90.9%. On the 37 completions where they did not, 34.2%, and those 37 carry **17% of all the
disagreement** despite being 2.7% of the items. The remaining gap is smaller than the headline makes
it look.

And one reassurance: across positions in a shuffled packed request the spread is **1.0 point**
(89.7%, 88.9%, 88.9%, 89.9% for items 1-8, 9-16, 17-24, 25-32), with no trend. The 3.3 point spread in
the position table above is the sorted-queue case, which is the one to avoid.

```bash
TYPESAFE_API_KEY=... python bench/confidence.py     # collects for ~12 cents, then argues for free
python bench/confidence.py --analyse data/results/<file>
```

`triage` ranks by `answer.certainty`, which for a yes/no question is not `p`: `p` is the probability
of yes, so a confident no has a low one. The table above is from a pick-one question, where the two
are the same number, and it was recomputed both ways to be sure: 96.78% either way.

The run behind that table is committed, so the analysis can be re-cut or argued with **with no key
and no spending**:

```bash
python bench/confidence.py --analyse data/results/20260920_122545_confidence_jev-latest_1347x6_p32_s7.jsonl
```

It holds one line per judgement: which completion, the human label, whether the two annotators agreed,
and what Jev said with what probability. No completion text, so it does not republish the pod.

### Finding your own cut

The threshold is a property of your question and your items, not of this library, and the three
corpora above are the proof: 0.92, 0.77 and 0.95, with the miscalibration changing sign along the
way. Copy a number out of that table and you will keep half of one task and nearly all of another.

So don't copy one. `calibrate` is the measurement above, over your rows, in one call:

```python
from jev_ultralightspeed import calibrate, classify, triage

cal = calibrate(rows[:300], labels[:300], question, accuracy=0.95)
print(cal)

answers = classify(everything, question)
trusted, review = triage(answers, at_least=cal.cut)
```

```
cut 0.730, keeping 85% at 94.9% agreement
  without a cut            91.0%
  with it                  94.9%  (+3.9 points)
  over 20 held-out splits  94.0% to 96.5%
  measured on 3,270 labelled rows
  note: you asked for 95.0% and rows the cut had not seen came in at 94.9%. Fitting on
        everything flatters the cut by about that much, so the held-out figure is the
        one to plan with
```

Say either `accuracy=` (how right it has to be, and it tells you what that costs in coverage) or
`keep=` (how much you want to automate, and it tells you what you get). A few hundred labelled rows
is enough and the call costs cents. `Calibration.from_answers(answers, gold, ...)` does the same on
answers you already have, for nothing.

**The cut is fitted on all your rows and the estimate is not.** Those are different questions. The
cut you deploy should have seen everything you have; what to *expect* from it has to come from rows
it did not see, or it is not a measurement. So the cut is chosen once on the lot, and the accuracy
and coverage come from twenty random half-and-half splits. When the held-out figure lands under the
bar you asked for, it says so, as above: that gap is the self-flattery, and it is the number to plan
with.

It also declines to tell you what is not there. Given certainty that carries no information it
refuses rather than returning a threshold fitted to noise, and it notes when there are too few rows
to say much, when the judge is so sure of everything that the ranking has nothing left to sort (the
AG News case), and when the bar you asked for is only reachable on a sliver of the pile.

#### The question to ask before "where do I cut"

A threshold sorts by certainty and keeps the top of the pile. So before it can mean anything, the
certainty has to know which answers are wrong. That is one number, and it is not accuracy and not
calibration:

```python
from jev_ultralightspeed import discrimination

discrimination(answers, labels)      # 0.87
```

The area under the ROC curve over (certainty, was it right). **1.0 is perfect separation, 0.5 is a
coin flip.** `calibrate` measures it first and **refuses** below 0.60, because under that there is
nothing for a cut to find and any gain it reported would be the sample flattering itself:

```
JevError: this judge's certainty separates right answers from wrong ones at 0.538, where 0.5
is a coin flip and anything under 0.60 is treated as none. A threshold sorts by certainty, so
on these rows there is nothing for one to find, and a gain would be the sample flattering
itself. It agrees 61.2% of the time overall. Either this judge cannot do this task or the
question needs rewording; pass min_signal lower to get the number anyway.
```

**This is not visible from the answers.** A judge can hand back ordinary-looking certainties, well
spread, none of them extreme, and still be at 0.5. Nothing in the response says which. Only labels
reveal it, which is why both of these take them, and why a few hundred labelled rows of your own are
worth more than any number in this README.

On a run that clears the bar, the figure comes back on the result (`cal.discrimination`) and prints
with it, so you can see how much the cut had to work with.

#### The other question: can a cut exist at all

`discrimination` asks whether the ranking knows right from wrong. A judge can pass that and still be
impossible to threshold, and the reason is worth knowing because AUROC cannot see it: **tied ranks
are averaged**, so a judge that ranks its work properly and one that reports a single number for two
thirds of it score the same. A cut cannot average anything. It keeps a whole block of equal
certainties or none of it.

Judges crowd. Measured on 3,270 rows from one provider, **65% came back at exactly 1.000**, and that
block was 96.5% accurate on its own:

```python
from jev_ultralightspeed import resolution

grain = resolution(answers, labels, accuracy=0.97)
print(grain)
# 61 distinct certainties, 65% of answers tied at 1.000
#   that block is 96.5% accurate on its own
#   at 97% a cut can keep at most 0% of the pile
grain.blocked                        # True
```

Zero, not "expensive". At a 97% bar there is no cut on that run at any coverage, even knowing every
label in advance, because the one block a cut would have to take is half a point short. Its pooled
AUROC was a healthy 0.785 throughout.

`calibrate` checks this itself and says which of the two problems you have, because the fixes are
opposite:

```
JevError: no cut reaches 97% on these rows, and more labels will not change that. 65% of the
answers are tied at 1.000 and that block is 96.5% accurate on its own, so a cut either keeps
all of it or none of it and neither clears the bar. The judge's certainty is too coarse here,
not too weak: it reported 61 distinct values over 3,270 rows. Ask each item several times and
average, which splits the block, or ask for less than 97%
```

**More labels cannot help a crowded pile, and more certainty can.** Asking each item several times
and averaging splits the block: on that run six asks turned 61 distinct values into 259, and
coverage at a 97% bar went from nothing to 59.8%. It buys almost nothing on AUROC, every paired
bootstrap interval straddling zero, so this is the one thing repeats are for.

When the bar is reachable the block still shows up as a note on the result, because it sets how
finely you can cut and therefore how lumpy your coverage will be.

#### The third question: which of its own answers can you trust

One cut assumes the certainty means the same thing whatever the judge said. On a real model it did
not. Split by the label it output, which is the only one of the two you have at inference time:

```python
from jev_ultralightspeed import routing

for verdict in routing(answers, labels, accuracy=0.95, confidence=0.90):
    print(verdict)

# says compliance   n=971     66.4% right   AUROC 0.404   inverted   certainty runs backwards here
# says partial      n=191      6.3% right   AUROC 0.537   send       cannot sort this class
# says refusal      n=185     97.8% right   AUROC 0.993   take       already clears 95%
```

Pooled, that judge is 62.2% accurate at AUROC 0.540, which reads as one not worth using. Per label
it is three different products: **one class you can automate outright, one you must never threshold,
and one to send to a person.** The pooled number is an average over all three and survives contact
with none of it.

Four verdicts, and only one of them is a cut:

| | |
| --- | --- |
| `take` | the class already clears your bar on its own lower bound. No cut needed |
| `cut` | the certainty sorts within the class, so threshold it there |
| `send` | the bar is out of reach here, or the certainty cannot sort it. Hand the class over |
| `inverted` | **more confident is more wrong.** Do not cut on it |
| `unknown` | fewer than 100 answers in the class, so there is nothing to say yet |

`inverted` is the one to stare at. Triaging on certainty inside such a class keeps precisely the
answers you would most want caught, and no threshold fixes it, because it usually means a systematic
confusion rather than noise.

`calibrate` runs this itself and adds a note when a judge's labels disagree about which direction
certainty points, so you are told even if you never call `routing`.

#### An estimate, or a guarantee

By default `accuracy=0.95` finds the longest prefix whose **observed** rate hits 95% on your rows.
That overshoots by however much the sample happened to flatter it, so the cut lands *on* the bar and
deployment is a coin flip either side. `Calibration.accuracy` reports that honestly, which is why
the held-out figure is the one to plan with.

When the cut runs unattended and the bar is a promise rather than a hope, ask for a bound instead:

```python
cal = calibrate(rows, labels, question, accuracy=0.95, confidence=0.95)
```

It then picks the lowest cut that clears 95% on the **low end of a Wilson interval**, so it cuts
higher, keeps less, and lands above the bar rather than on it. Confidence is one of 0.5, 0.8, 0.9,
0.95 or 0.99, and it applies to `accuracy=` only: with `keep=` the cut is set by the share you asked
to keep, so there is no bar to be confident about.

It will refuse when your rows cannot support the bound, and say so in the bound's own terms rather
than quoting an observed rate that looks like it contradicts the refusal. At a few hundred rows the
binding constraint is usually the count, not the judge.

`cal.headroom` is what the margin is costing: the coverage the observed rate would have allowed,
minus what the bound allows. Wide means more labels would buy you coverage. Narrow means the data
has already given up everything it has.

### Carrying the question once

```python
client = Client(pack=32, guidance="once")      # the default is "repeat"
```

A packed request writes the whole question into every item's question. Thirty-two items means
thirty-two copies of the instructions and the criteria, which at `pack=32` is **74.5% of the body**,
and it is why bytes per item are flat however deep you pack. `guidance="once"` puts the question in
the state under one key and has each question point at it. For a pick-one question the option names
stay where they are, because they are the answer space rather than wording.

**What it saves depends entirely on your items.** The saving is the ratio of question text to item
text, so it is large when the question is long and the rows are short, and nearly nothing the other
way around:

| | billed input tokens per item | saving |
| --- | ---: | ---: |
| a long yes/no question, short support messages | 143.7 → 55.5 | **61%** |
| a one-line pick-one question, long model completions | 384.8 → 374.6 | 3% |

**What it costs.** 8,082 judgements over the 1,347 completions in the `xstest-refusal` pod, both arms
packed 32 deep over the same items in a randomised arm order:

| | the question in every item | the question once |
| --- | ---: | ---: |
| agreement with the human labels | 89.3% (87.7 to 90.8) | 89.1% (87.4 to 90.6) |
| same answer across an item's repeats | 98.2% | 98.1% |

Paired over the completions, once minus repeat is **−0.20 points, 95% −0.41 to +0.01**, and 1.0% of
individual verdicts move. That is inside the two point margin, so the honest summary is "no
difference worth caring about at this sample size". But the interval sits almost entirely below zero,
which is a hint of a real effect of about a fifth of a point against the shared question, and one
pod's short question is a smaller prompt change than a long one would be. So it is opt in, it is part
of the cache and checkpoint key, and `bench/guidance.py` reruns the comparison for about 25 cents.

### Resuming a job that dies

A million rows take half an hour and thirty-one thousand requests. Something will eventually
kill one of those runs at row 800,000, and paying for those 800,000 answers twice is the expensive
kind of mistake. Pass a `checkpoint` and it cannot happen:

```python
for answer in client.stream(rows, question, checkpoint="run.jsonl"):
    writer.writerow([answer.item, answer.label, answer.p])
```

`stream()` hands each answer over as the request it rode on lands. `chunk` bounds how much is held in
memory and how far deduplication looks, not when you hear anything: within a chunk the requests roll,
and an answer is out as soon as everything before it is known. Only a repeat whose original is still
in flight waits, and it waits for that one request. With 256 items, one slow request near the end and
eight workers:

| | first answer, before | first answer, now | whole job |
| --- | ---: | ---: | ---: |
| threads | 1.48s | **0.06s** | 1.48s → 1.50s |
| http2 | 1.68s | **0.23s** | 1.68s → 1.69s |

The job takes the same time, which is the point worth being clear about: this is about when answers
arrive, not how quickly the work finishes. What it buys is downstream work overlapping the API,
checkpoint writes spread through the run instead of arriving in 5,000-row jolts, and more banked when
something kills the process.

Every answer is appended to that file as its request lands. Run the same call again after a crash, a
kill or a laptop lid, and anything already in the file is not asked again: `client.usage.resumed`
says how many came back off disk. It is plain JSON lines, so `wc -l` tells you where you are.

It is keyed by exactly what the answer depends on, which is the model, the question, the criteria
and the item text. Change any of them and the item is asked again, because the old answer is an
answer to a different question. Measured on a checkpoint of a million answers:

| | |
| --- | ---: |
| file | 119 MB |
| replayed on startup | 1,000,000 answers in **5.0s** |
| index held in memory | 123 MB |
| written | about 95,000 answers a second |

Writing is roughly two hundred times faster than the API can answer, so the sidecar is never the
thing slowing you down. The index holds a 128-bit digest and a file offset per answer and never the
item text, which is what keeps a million rows inside a laptop: the answer itself is read back off
disk when it is wanted. A hard kill can lose the last couple of hundred answers still in the write
buffer, and a torn final line is repaired on the next open.

`pack` is part of the key, because an answer produced 32 deep is not the answer you would have got
one at a time: the position table above measures 3.3 points between the first eight items of a
request and the last eight. Change `pack` and the items are asked again rather than being served an
answer from a different depth.

### Finishing around the rows you cannot do

A durable job and a bad row are a bad combination. One item whose answer will not parse ends the
run; the checkpoint means the rerun resumes, reaches the same row and ends in the same place, and it
does that forever without telling you which row. `on_error="skip"` is the way out:

```python
answers = client.classify(rows, question, checkpoint="run.jsonl", on_error="skip")
bad = [answer for answer in answers if not answer.ok]
```

An item with no readable answer, and a request that failed for good, leave an `Answer` in place with
`ok` False and `error` saying why. They are listed in `client.failures` and counted in
`usage.skipped`, and they are deliberately **not** written to the checkpoint, so the next run tries
them again rather than banking "no answer" forever.

It is a budget, not a blanket: up to one percent of the items in a call may go this way, or five
requests' worth, whichever is larger. Past that the run is abandoned and raises, because a wrong key
must not be quietly skipped a million times. A test holds that line.

## What it does not do

- **It does not change your question.** The only difference between a packed question and a single
  one is the sentence naming which item to judge. There is no bitstring trick and no compressed
  output format, because Jev returns a structured probability per question rather than generated
  text: the output is about twenty tokens **per item**, measured, which is 148 for a request of
  eight. Nor is there a prefix to cache: the same body sent twice billed 2,346 input tokens both
  times, and `usage` carries only `input_tokens` and `output_tokens`. Every dollar figure here counts
  input tokens at TypeSafe's published $0.042 a million, because that is the price they publish. If
  output is billed separately the figures are low by whatever that costs, and output is about 5% of
  the tokens on this pod.
- **It does not defend against what is inside your items.** Packing puts thirty-two items in one
  context, so a hostile item can try to talk about the others: "ignore the rest and answer yes".
  Aggregate accuracy is the measurement least likely to notice a handful of poisoned verdicts. Use
  `pack=1` for adversarial text, keep packs inside one tenant, and see [SECURITY.md](SECURITY.md).
- **It does not cache across processes unless you ask it to.** The cache lives in the client, in
  memory, bounded at 10,000 entries. A `checkpoint` is the durable version of it, and the two share
  a key.
- **The cache key holds the `pack` you asked for, not the one an answer came back from.** A call of
  33 items at `pack=32` sends a request of 32 and a request of 1, and that lone answer is filed under
  the same key as the other 32; ask again inside a full pack and the lone answer is what you get.
  Placement cannot be in the key, because it is not a function of the input: deduplication and cache
  hits change the grouping, so the same call twice can put the same row in a different sized request,
  and a key that depended on it would miss almost every time. What is traded away is measured rather
  than assumed: packed against one per request is **+0.01 points, 95% −0.72 to +0.75**, and no
  position effect is detectable at any depth. Every answer carries the depth it came from in
  `answer.packed`, and `cache=False` with `dedupe=False` and no checkpoint is how `bench/packing.py`
  controls placement when it has to.
- **It does not hide failures.** Retries cover 429, 500, 502, 503, 504 and 529, with jitter and the
  server's own `Retry-After` when it sends one; anything else is raised with what the API said. A
  failure that is not going to be skipped abandons the run rather than sending the rest: a wrong
  key over 200 items sends 8 requests on the fast path and 9 on the threaded one, counted by a test
  against a local server that refuses everything, rather than estimated. Work already finished is kept: `stream()`
  yields each chunk as it completes, after a failed call `client.last_partial` holds the answers
  that did arrive on either transport, and with a `checkpoint` they are already on disk.
- **It is not an eval harness.** It makes a judge fast, and it will tell you which of its verdicts
  to distrust, but it does not tell you whether the judge is the right judge. See Related below.
- **It is not the cheapest way to do a backfill nobody is waiting for.** If TypeSafe offers an
  offline batch endpoint, it will beat everything here on both price and ceiling, because none of
  this can buy a rate limit. This is for work that has to happen now.

## Development

```bash
pip install pytest
python -m pytest tests -q        # 218 tests, a local server, no key and no network needed
JEV_PROPERTY_ITEMS=50000 python -m pytest tests/test_properties.py   # the volume ones, bigger

TYPESAFE_API_KEY=... python bench/eval.py            # the table above, ~35 min, ~$1.20
python demo.py                                      # the two arms racing, 30s, no key
python bench/sweep.py --offline --items 8000 --rounds 9    # the client's own work, no key, no calls
TYPESAFE_API_KEY=... python bench/workers.py         # latency against concurrency, ~10 cents
TYPESAFE_API_KEY=... python bench/trim.py            # how much of an item is needed, ~15 cents
TYPESAFE_API_KEY=... python bench/score.py           # a score against human labels, ~7 cents
TYPESAFE_API_KEY=... python bench/sweep.py --items 256     # pack and concurrency sweep, ~5 cents
TYPESAFE_API_KEY=... python bench/packing.py         # position and sorted queues, ~$1
TYPESAFE_API_KEY=... python bench/guidance.py        # the question once vs per item, ~25 cents
TYPESAFE_API_KEY=... python bench/sustained.py       # is the rate a rate, 10 min, ~$1.60
TYPESAFE_API_KEY=... python bench/soak.py --items 100000   # sustained load, ~50 cents

python bench/corpora.py --build boolq ag_news              # the labelled sets, downloaded once
TYPESAFE_API_KEY=... python bench/confidence.py --corpus boolq    # triage on one, ~15 cents
python bench/confidence.py --compare data/results/*confidence*.jsonl   # all of them, free
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
