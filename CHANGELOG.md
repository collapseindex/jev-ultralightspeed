# Changelog

## v0.9.0 (2026-09-20)

Throughput is within a fifth of its arithmetic ceiling, so this release is about the other gap.
Agreement with human labels is **eight points** below the humans' agreement with each other, 89.3%
against 97.3%, and that turns out to be mostly recoverable without asking Jev anything different.

### Added
- **`triage(answers, keep=0.8)`**, or `at_least=0.92`. Splits answers into the ones worth trusting and
  the ones worth a look, by the judge's own probability. 8,082 judgements over the 1,347 completions in
  `xstest-refusal`, with the cut chosen on one half of the completions and measured on the other:

  | kept | cut at | agreement |
  | ---: | ---: | ---: |
  | 100% | | 89.7% |
  | 89% | 0.770 | 94.4% |
  | **81%** | **0.920** | **96.8%** |
  | 72% | 0.970 | 98.5% |
  | 62% | 0.990 | 99.3% |

  Set aside the fifth it is least sure about and the rest is at the annotators' own agreement rate.
  Not the same measurement on the same set, so it reads as "at the ceiling on the part it is sure
  about" rather than "as good as a person", and the README says so. A skipped answer is always one to
  look at, and both lists keep their input order.
- **`bench_confidence.py`**, which collects for about 12 cents and then lets the analysis be argued
  with for free: every judgement is written to `data/results/` so a threshold can be re-cut without
  paying again. It reports risk and coverage both in-sample and held out, calibration, unanimity,
  voting, position, and the split by whether the two annotators agreed with each other.

### Measured, including two negative results
- **Asking the same item six times buys nothing.** Majority of six 89.5%, a single ask 89.7%. Six
  times the tokens for a fifth of a point in the wrong direction. Self-consistency voting is the first
  thing anyone reaches for on a task like this, and here it is a waste.
- **Wavering is a signal even though voting is not.** The 1,256 completions answered the same way all
  six times: 92.7%. The 91 where it did not: 43.4%.
- **`p` ranks but is not a chance of being right.** Where it said 74.8% it was right 51.0% of the time;
  where it said 92.3%, 78.4%. Weighted across the bands the gap averages 4.4 points. The docstring on
  `Answer.p` now says to sort by it and not to read it as a percentage.
- **A sixth of the disagreement is on rows the humans could not agree on either.** 90.9% where both
  annotators agreed, 34.2% on the 37 where they did not, and those 37 carry 17% of all the
  disagreement while being 2.7% of the items.
- **Position in a shuffled packed request costs 1.0 point of spread**, no trend: 89.7%, 88.9%, 88.9%,
  89.9% across items 1-8, 9-16, 17-24, 25-32. The 3.3 point spread published earlier is the
  sorted-queue case, which remains the one to avoid.

94 tests, no key and no network needed.

## v0.8.0 (2026-09-20)

Two things that were named in the README as known shortcomings and are now done: the question is no
longer repeated once per item unless you want it to be, and `stream()` no longer holds a chunk's
answers back until the chunk is finished.

### Added
- **`guidance="once"`.** A packed request writes the whole question into every item's question, which
  at `pack=32` is **74.5% of the body** and is why bytes per item are flat however deep you pack. This
  carries it in the state under one key instead. On a live 32-item request with a long yes/no question
  that took the billed input from **4,598 tokens to 1,777**, a 61% saving. On the `xstest-refusal`
  pod, where the completions are long and the question is one line, it saves 3%. The saving is the
  ratio of question text to item text and nothing else, and the README says so.

  **Measured against the answers, not assumed.** 8,082 judgements over 1,347 completions, both arms
  packed 32 deep over the same items in a randomised arm order: 89.3% agreement with the human labels
  the old way, 89.1% the new way, paired difference **−0.20 points, 95% −0.41 to +0.01**, with 1.0% of
  individual verdicts moving. Inside the two point margin, so "no difference worth caring about at
  this sample size". But that interval sits almost entirely below zero, which is a hint of a real
  effect of about a fifth of a point, so it is opt in rather than the default, and it is part of the
  cache and checkpoint key because a different prompt is a different answer. `bench_guidance.py`
  reruns it for about 25 cents. Checkpoint format 3.

### Changed
- **`stream()` rolls instead of waiting for the chunk.** It used to classify a whole chunk and only
  then yield any of it, so the first answer of a 5,000-item chunk waited on all 157 of its requests.
  Now `chunk` bounds memory and how far deduplication looks, and answers leave as the requests they
  rode on land. With 256 items, one slow request near the end and eight workers:

  | | first answer, before | first answer, now | whole job |
  | --- | ---: | ---: | ---: |
  | threads | 1.48s | **0.06s** | 1.48s → 1.50s |
  | http2 | 1.68s | **0.23s** | 1.68s → 1.69s |

  The job takes the same time, and that is the honest summary: this changes when answers arrive, not
  how fast the work finishes. What it buys is downstream work overlapping the API, checkpoint writes
  spread through the run rather than arriving in 5,000-row jolts, and more banked if the process dies.
  A repeat still waits for the original it copies, because order is the promise, but it waits for that
  one request rather than for the chunk.
- `classify()` and `stream()` are now the same engine, which reads each payload where it lands, banks
  in completion order and yields in input order. Durability should not queue behind a straggler and
  the caller's order is the promise, so those two are deliberately different orders.
- `stream()` takes `on_progress` too.

### Fixed
- A found-while-building bug, and a good one: abandoning a run cancelled the requests already in the
  air, which left their tasks stuck in `cancelling` and the event loop stopped with a pending future
  behind it. That hung about one run in three. Anything that has not sent is told to stop and raises;
  anything already sent is given five seconds to come back. The batch path never cancelled a sibling
  either, and for the same reason.

88 tests, no key and no network needed.

## v0.7.0 (2026-09-20)

This package claims its fast path is about twice as quick because every request in flight shares one
HTTP/2 connection. Every test it had ran against an HTTP/1.1 server, and httpx falls back to HTTP/1.1
whenever ALPN does not offer h2 without saying so, which means that claim was taken on faith through
seven releases. It is now checked against a real h2 server, and checking it found a bug.

### Added
- **A test that runs the fast path over real HTTP/2**, on loopback, with TLS and a certificate
  authority of its own: 16 requests for 64 items arrive on **one** connection with more than one
  stream open at a time, and the negotiated protocol is asserted rather than assumed.
- **`verify`**, a CA file or an `ssl.SSLContext`, for a gateway signed by a private CA. `url` has
  always invited pointing this at your own gateway and there was no way to trust one. Never a
  boolean: switching verification off is something a caller should have to write out.
- **`client.http_version`**, what the connection actually negotiated. A proxy that strips ALPN costs
  you the whole reason for installing `[fast]`, silently, and there was no way to find out.

### Fixed
- **A caller's own TLS context was never offered h2.** httpx sets ALPN on the contexts it builds and
  not on one it is handed, so anyone passing a context for a private CA would have quietly dropped to
  HTTP/1.1. Found by the new server, which offers h2 and nothing else.
- **`isinstance(x, ssl.SSLContext)` is not a reliable check.** `ssl.SSLContext` can be replaced at
  run time, and pip's vendored truststore does exactly that, so a plain stdlib context was not
  recognised as one. The base class out of the MRO is what to test against.

### Notes
Two of the 77 tests skip on a machine whose TLS is intercepted, and they say so rather than passing
quietly: the machine this was written on has Norton re-signing even 127.0.0.1 with a root it then
declines to trust, so no local TLS server can be reached at all. That is also the explanation for the
`CERTIFICATE_VERIFY_FAILED` in v0.1.1. CI has no interceptor, and the fast job now names those tests
so a skip there would be visible.

77 tests, no key and no network needed.

## v0.6.0 (2026-09-20)

A scheduling review found that waiting was the problem in four separate places: waiting for a
straggler, waiting for permits nobody would use, waiting to notice an error, and not waiting at all
where a token limit needed respecting. Every number below is reproduced by a test that fails on
v0.5.0.

### Fixed
- **A copy of a skipped item claimed to have an answer.** `_copy_answer` left `error` behind, so the
  second of two identical rows came back with `ok` True, `kind` "error" and nothing in it, and was
  counted as cached goodput. The error travels with the copy now, and it is counted as skipped.
- **A straggler held back everything behind it.** The threaded path consumed `pool.map` in input
  order, so one slow request delayed the banking, the progress and the error of every finished
  request behind it. Measured: the first progress report arrived **0.62s** into a run where fifteen
  of sixteen requests took 10ms. It is a bounded rolling window now, two requests queued per worker,
  banking each group the moment it lands.
- **A fatal error went unnoticed until the iterator reached it.** Behind one slow first group, the
  pool churned through the rest while nobody was looking: **64 of 64** requests went out on a run
  doomed by the second. Now the failure is seen at completion, the queue is dropped and nothing
  unstarted is waited on.
- **The fast path spent permits it was not about to use, then drained them after giving up.** A
  permit was taken before the send slot, so with a hundred bodies and two slots every permit was
  spent by the third send and the limiter's timestamps were not send times. The permit is now taken
  inside the slot, immediately before the request. Waiting happens in slices and checks whether the
  run is over, so an abandoned run stops waiting as well as sending: with permits scarce it used to
  drain all hundred before returning.
- **Threaded `warm()` opened nothing.** Constructing an `HTTPConnection` connects lazily, so warming
  four workers made **zero** handshakes. It connects now, under the warm-up deadline rather than the
  request timeout.
- **`Retry-After` only understood one of its two forms.** RFC 9110 allows an HTTP date, which was
  read as no hint at all. Both forms are parsed, and the server's number is a floor with jitter on
  top rather than an exact wait, because every worker given the same hint came back at the same
  instant.
- **`pack` counted items where the API counts tokens.** TypeSafe documents 64k tokens in a request
  and 32k for the state plus the longest question, so several individually legal items could make one
  illegal request, and raising `pack` made it likelier. Groups are planned against both limits at a
  pessimistic 3.5 characters per token, against 3.92 measured live. Short items still pack to `pack`.
- Idle connections on the fast path expired after httpx's default 5 seconds, so every call in an
  intermittent job paid a handshake. Now 60.

### Changed
- **A claim in the last release was wrong and is corrected in place.** v0.5.0's README said 245
  retries at 9M input tokens a minute proved a token-counted limit was binding. TypeSafe documents
  250,000 tokens a second, which is 15M a minute, so 9M is *below* the published ceiling on average
  and bursts, allocation changes or plain overload explain the pushbacks equally well. That run kept
  no status codes, so it cannot tell them apart.
- The README now says where the ceiling is. At `pack=32` and 1,000 requests a minute the request-only
  maximum is **533 items/s**, and the measured 441 is 21% below it, so there is no large win left in
  the transport. It also says this is not compute-bound: about 600 KB/s of JSON, so `orjson` and
  `uvloop` have nothing to do here.
- Recorded a measurement that is not shipped: the question block is repeated per item, 74.5% of a
  `pack=32` body, and carrying the guidance once in `state` cut a live 32-item request from **4,598
  to 1,777 billed input tokens** with all 32 labels unchanged. Not the default, because a prompt
  change needs the paired accuracy run first and one request is not an accuracy result.

74 tests, no key and no network needed. Two of them are about this file: the README badge sat at
v0.3.1 through three releases because the bump touched `pyproject.toml` and `__init__.py` and nothing
checked the other two places, and the test count in the README had been hand-edited five times. Both
are asserted now rather than remembered.

## v0.5.0 (2026-09-20)

v0.4.0 made a job durable and gave it no way to get past a row it could not do, and those two
features fought each other: the more durable the job, the more permanently one bad row could wedge
it. Reproduced over 1,000 items with one unparseable answer, resuming from a checkpoint each time:

```
run 1: died -> unreadable answer  | banked 992
run 2: died -> unreadable answer  | resumed 992, banked 992
run 3: died -> unreadable answer  | resumed 992, banked 992
```

Permanently stuck at 992 of 1,000, and nothing said which row. That is fixed, along with three other
things a review found.

### Added
- **`on_error="skip"` on `classify()` and `stream()`.** An item whose answer will not read, and a
  request that failed for good, leave an `Answer` in place with `ok` False and `error` saying why,
  and the run carries on. They are listed in `client.failures`, counted in `usage.skipped`, and
  deliberately **not** written to the checkpoint, so the next run tries them again rather than
  banking "no answer" forever. The same 1,000 items now finish on both transports: 999 banked, one
  named, and the rerun re-asks exactly that one.
- It is a budget, not a blanket: one percent of the items in a call, or five requests' worth,
  whichever is larger. Past that the run is abandoned and raises, because a wrong key must not be
  skipped a million times over. Measured: a wrong key under `on_error="skip"` over 1,000 items gives
  up after 40 items on both transports.
- `Answer.ok` and `Answer.error`, and `Usage.answered` and `Usage.skipped`.

### Fixed
- **The checkpoint key ignored the pack depth.** A `pack=1` rerun was served answers produced at
  `pack=32`, which `bench_packing.py` measures as 3.3 points apart by position. `pack` is in the key
  now, so changing it asks again. Checkpoint format 2; a format 1 file is refused with a message
  saying why rather than silently reused.
- **`usage` lied on a resumed run.** Resumed and cached items counted toward `items_per_second`,
  which reported *14.5 million items a second* for a run that asked nothing. Rates are over
  `usage.answered` now, which is the items that actually cost a request. This is the repo where
  `usage` is the evidence behind every claim, so it was the one number in it that was not true.
- **The threaded path did not flush its checkpoint before raising** where the fast path did, which
  is exactly the asymmetry the `both_transports` decorator exists to catch.
- **`GIVE_UP_AFTER = 5` was an absolute count**, so five post-retry failures abandoned a run whether
  it was 200 requests or 31,400: a 0.016% failure tolerance on a million rows. The threshold is gone.
  A failure the caller will not skip now abandons the run immediately, which is strictly better,
  because in that mode the call is going to raise anyway and the rest of the requests are waste. A
  wrong key over 200 items now sends 8 requests on the fast path, down from 12, and 9 on the
  threaded one.

### Changed
- The README says what the 26.5x is. The packed arm sat at 831 requests a minute, under the
  published 1,200, and still took 245 retries while pushing about 9M input tokens a minute against
  the baseline's 578K. So the headline is the gap between *which* ceiling each arm hits, and it is a
  ceiling rather than a floor. What does not depend on anyone's rate tier is the 41% token saving,
  about **1.7x**. Both numbers are now in the README with that said plainly.
- The README also names a confound it had not: a packed request ends with "Judge item_N only,
  ignoring every other item" and an unpacked one has no reason to, so the two benchmark arms differ
  by one sentence as well as by shape. The paired interval is wide enough to absorb it. It is still
  not zero.

63 tests, no key and no network needed.

## v0.4.0 (2026-09-20)

The README has said "for when the question already works and there are a million rows waiting" since
the first release, and until now a job of a million rows that died at row 800,000 lost everything.

### Added
- **`checkpoint`, on `classify()` and `stream()`.** An append-only JSON lines sidecar. Every answer
  is written as its request lands, and a rerun of the same call skips whatever is already in the
  file, so a killed run resumes instead of starting over. `usage.resumed` counts what came back off
  disk. It is keyed by what the answer actually depends on, which is the model, question, criteria
  and item text, so changing any of them asks again.

  Measured on a checkpoint of a million answers: a 119 MB file, replayed on startup in **5.0
  seconds**, holding a **123 MB** index, written at about **95,000 answers a second**, which is
  roughly two hundred times faster than the API can answer. The index holds a 128-bit digest and a
  file offset per answer and never the item text, so a million rows fit on a laptop; the answer
  itself is read back off disk when it is wanted. A hard kill loses at most the couple of hundred
  answers still in the write buffer, and a torn last line is repaired on the next open.
- `soak.py --checkpoint`, which is the script the feature exists for. Kill it and run it again.

### Changed
- The fast path reads each payload on the thread it lands on, once, rather than again at the end of
  the run. That is what lets a checkpoint be durable before the run can die, and it is also why
  `_http2.Pipe` no longer needs an `on_failure` hook: whatever landed is already with the caller.
  Internal, but it is a smaller interface than v0.3.2 shipped.
- `last_partial` is `list[Answer]` on both transports, which v0.3.2 fixed, and with a checkpoint it
  is on disk as well as in memory.

### Fixed
- A checkpoint line torn mid-digest by a hard kill produced a short key that looked like a perfectly
  good one for an item nobody had asked about. Found by its own test.

54 tests, still no key and no network needed. One of the new ones checks that a payload one
answer short names the missing item identically on both transports, since reading moved to the
thread each request lands on.

## v0.3.2 (2026-09-20)

Both fixes here are the same shape as the last release's: something that worked on the threaded
path and quietly did nothing on the fast one. The tests are the real change. Every behavioural test
now runs on both transports through one `both_transports` decorator, against a threading server on
loopback, so a whole transport can no longer go unchecked. Stashing this release's source and
running the new tests against the old confirms they fail on `[http2]` and pass on `[threads]`,
which is exactly the shape of the blind spot.

### Fixed
- **`on_progress` reported nothing until the run was over on the fast path.** Progress was reported
  while reading the payloads, which happens after every request has already landed, so a progress
  bar went from nothing to done in one step. `Pipe.ask_all` now takes a per-request callback that
  fires as each request lands. Measured over 40 items at 300ms each: the first report used to
  arrive at 3.099s of 3.100s, and now arrives at **0.478s of 3.268s**. It runs on the pipe's loop
  thread, which is documented on `classify`.
- **`last_partial` was a different type on each transport.** The fast path handed back surviving
  raw payloads with the failures filtered out, so there was no way to tell which items they
  answered; the threaded path handed back `list[Answer]`. `gather` now keeps each payload's index,
  so survivors are matched back to their own items and both paths return `list[Answer]`. An
  unreadable group is dropped rather than raising over the error already on its way up.
- **`warm()` on the fast path skipped the rate limiter.** A client that warmed up was one request
  over its own budget before any work arrived. The warm-up now takes a slot like anything else.

### Changed
- The README reads 89% against a ceiling rather than against 100%: the pod's two annotators agreed
  with each other on 1,310 of 1,347 completions, so **97.3%** is the number both arms sit under.

Thanks to the reviewers who found the first two. Neither was reachable from the old suite.

## v0.3.1 (2026-09-20)

A review reproduced two of the last two releases' headline fixes not working. Both reproduce, both
are fixed, and the repros are in the suite now.

### Fixed
- **Failing fast did not work on the fast path.** The abandon check sat at the top of each
  attempt's loop, but `gather` starts every coroutine at once, so all of them passed it before the
  first failure had happened; and since 401 is not retryable there was never a second attempt where
  it would fire again. A wrong key sent **200 of 200** requests. The check now sits inside the
  semaphore, immediately before the request: **12 of 200**, and 9 on the threaded path.
- **The number in the 0.2.1 changelog was not measured.** "Five requests" came from wall clock on a
  small run; `usage.requests` counts successful requests, so the figure I quoted counted nothing at
  all. The suite now counts what leaves the client, against a local server that refuses everything.
- **`dedupe=False` did not give independent repeats.** The cache is checked first and defaults to
  on, so across `stream()` chunks the repeats came back from memory: 40 identical items sent 10
  requests and served 30 copies, which looks like near-perfect consistency and is the exact failure
  the flag was added to prevent. Turning off deduplication now turns off the cache with it.
- **The threaded path ignored the URL scheme**, always building an HTTPS connection, so a gateway
  or a test double on `http://` was unreachable on the transport a plain install uses.
- `Pipe.broken` was instance state, so two concurrent calls on one client stomped each other; it is
  per call now.
- Waiting on the rate limit parked threads of the default executor, which belongs to the host
  program. The pipe waits on its own.
- `last_partial` was not cleared at the start of a run on the fast path, so a success could carry
  stale payloads from an earlier failure.
- An oversized item now says which item, and that nothing has been sent.
- `ask()`'s docstring claimed a reopened connection was not counted as a retry. The code counts it.

## v0.3.0 (2026-09-20)

A fourth review asked two questions a throughput benchmark does not answer, so they were measured.

### Added
- **`bench_packing.py`, and the finding it produced.** 10,776 judgements, packed 32 deep, over a
  shuffled queue and over a queue sorted so each pack is full of near-identical items. A sorted
  queue is not worse in aggregate (89.4% against 89.3%) and is steadier (99.6% against 98.1%), but
  the position effect doubles: in the sorted arm the last eight items of a request scored 3.3
  points below the first eight. Shuffle before packing if your queue arrives sorted.
- **`position` and `packed` on every answer**, so anyone can check that on their own data instead
  of taking this repository's word for it.
- **`dedupe=False`**, because deduplication is a trap when the repeat is the measurement: with it
  on, asking the same item twenty times costs one request and returns twenty copies, which looks
  like perfect consistency and is not.

### Fixed
- `last_partial` was only filled on the HTTP/2 path, so what survived a failure depended on which
  transport you were using.
- `latencies` grew without bound on a long-lived client; the last 10,000 are kept.

### Changed
- SECURITY.md says the limiter is per process and counts requests, not tokens: four workers on the
  default ceiling is four thousand requests a minute against a limit of twelve hundred, and 942
  packed requests drew 245 retries while comfortably inside the request ceiling.

## v0.2.1 (2026-09-20)

A third review, mostly of the previous review's fixes.

### Fixed
- **The threaded path never got the retry manners the fast path did.** It still doubled without
  jitter and ignored `Retry-After`, and counted its retries outside the lock, which matters because
  the threaded path is what a plain `pip install jev-ultralightspeed` runs. Both paths now share
  one backoff.
- **Two limiters could hold two windows.** The pipe built its own from the same number instead of
  sharing the client's, so a client touching both paths could reach twice the ceiling: the exact
  bug the previous release was about. There is one limiter now, and the fast path waits on it
  before taking a slot rather than inside one.
- **Nothing failed fast any more.** Letting siblings finish instead of cancelling them meant a
  wrong key sent every request and took half an hour to say so. A run is now abandoned after five
  failures, so a wrong key costs five requests: measured at 2.1 seconds over 200 items.
- **Work finished before a failure was thrown away.** `client.last_partial` holds the payloads that
  arrived, and `stream()` already yields each chunk as it completes.
- `warm()` could block for a minute on a busy pool; it gives up after five seconds, because warming
  is an optimisation and never worth a wait.
- `Client` is a context manager, which matters now that it owns a pool, a connection and a thread.

### Changed
- **The throughput figures for the million-item run are withdrawn.** They were measured at about
  2,136 requests a minute, before the limiter reached the fast path, and no client honouring the
  published ceiling can reproduce them. Its cost figure stands, since the token arithmetic does not
  depend on the rate. The same applies to the pack-size throughput numbers in the knobs table.
- **The "How" list no longer claims an order of importance the rate limit has taken away.** Against
  a ceiling counted in requests, packing is essentially the whole 26.5x; the transport work is what
  keeps a packed run from spending its budget on handshakes, and it earns its keep again the moment
  the limit is raised. Both things are true and they are now stated separately.
- The test count, the benchmark's runtime and the hero image's alt text now match what they
  describe. 29 tests.

## v0.2.0 (2026-09-20)

### Fixed
- **The rate limit never reached the fast path.** `_limiter.take()` was called only by the threaded
  client, so the ceiling the README and SECURITY.md both promised applied to the transport almost
  nobody used. Every benchmark before this one was therefore measured at about 2,460 requests a
  minute, double TypeSafe's published 1,200: real numbers that a well-behaved client cannot
  reproduce. Both paths now hold the same ceiling, and the table has been remeasured.
- A request waiting out a 429 held one of the few in-flight slots while doing nothing; the waiting
  now happens outside the semaphore.
- Retries had no jitter and ignored `Retry-After`, so workers backed off together and collided
  again. The server's hint wins, otherwise it is doubling with jitter.
- One bad answer cancelled its siblings mid-flight, so a long run could return nothing having spent
  the money anyway. Every request now runs to its own end.
- `warm()` opened connections in a throwaway pool's thread locals and destroyed it, warming nothing
  and leaking connections per chunk. One pool now lives as long as the client.
- An async caller was silently given the threaded path while `transport` still said http2; it
  refuses with an instruction instead.
- A missing answer shortened the returned list rather than failing, quietly breaking the documented
  order guarantee.
- Cached and deduplicated answers shared one mutable distribution dict, in two places.
- `usage` was incremented from worker threads and the loop thread without a lock.
- The one-call `classify()` never closed its client.

### Changed
- **The accuracy comparison is paired and clustered.** Wilson intervals on 30,000 judgements
  treated 22 repeats of 1,347 completions as independent, reporting an interval about five times
  narrower than the evidence supports. `bench_eval.py` now computes each completion's own accuracy,
  bootstraps over the completions, and reports the paired difference: −0.09 points, 95% −0.83 to
  +0.61, inside a two point margin. "No difference this benchmark can detect", not "same accuracy".
- `stream()` says what it does: chunking, not answers handed back the moment each lands.

### Added
- SECURITY.md covers packing as an attack surface: thirty-two items share one context, a hostile
  item can address the others, and aggregate accuracy is the measurement least likely to notice.
- A CI job that installs the `[fast]` extra and asserts the HTTP/2 path is the one selected.
- Tests for all of the above, 26 in total.

## v0.1.1 (2026-09-20)

### Fixed
- The HTTP/2 transport now trusts the operating system's certificate store, the same one the
  standard library path uses. It trusted only certifi's bundle before, so on any machine whose TLS
  is inspected by a proxy or an antivirus with a root in the OS store, `[fast]` failed to connect
  while the plain install worked. Found by installing the published package into a clean
  environment and using it.

## v0.1.0 (2026-09-20)

On PyPI: `pip install "jev-ultralightspeed[fast]"`

First release.

### Added
- One benchmark instead of three: 30,000 judgements over the same human-labelled completions,
  reported as 15.9x the throughput, 41% less money, 89.3% against 89.2% agreement with the labels,
  99.6% against 98.0% repeatability, 0 failed, 1 against 63 retried. `bench_eval.py` runs it.
- Earlier, smaller runs over the same pod, kept for the record: 1,347 items gave 18x and 19.6x on
  two occasions, at 89.2% against 90.6% and 89.1% against 90.7%. The headline now comes from the
  30,000-judgement run above, which is one run, at scale, with every figure from the same arms.
- `usage.retries`, so "no failures" is a number the client counted rather than the absence of a
  complaint. Both transports report it.
- A million items in 14.7 minutes for $4.99, with none of the 31,400 requests failing or retried in
  that run, streamed to a CSV: 1,134.8 items/s
  over 31,400 requests, holding flat for the whole run, agreeing with the template each message was
  generated from 99.79% of the time. `soak.py` reproduces it.
- `Client.stream()`, which yields answers as they land and never holds more than a chunk, so a
  million rows costs the memory of five thousand.
- Measured on dinostomp's xstest-refusal pod: packing moves about 4% of individual verdicts against
  0.3% between two runs of the same shape, which is the same finding the 30,000-judgement run
  reports as repeatability, 99.6% against 98.0%.
- An optional HTTP/2 transport (`pip install "jev-ultralightspeed[fast]"`): one event loop, one
  connection, every request in flight multiplexed over it, kept open between calls. About twice the
  threaded standard-library path, which remains the fallback.
- `classify(items, question)` and a `Client` that packs several items into one request, keeps
  several requests in flight under a rate limit, reuses one connection per worker, deduplicates
  identical text and caches answers by model, question and text.
- Yes-or-no and pick-one questions, answers in the order the items were given, with the
  probability, the distribution and the confidence.
- `bench.py`, which reproduces the table in the README against your own key: warmed connections,
  shapes in random order, p50 and p95, tokens per item, and agreement with the one-at-a-time
  baseline.
- 18 offline tests covering packing, ordering, deduplication, the cache, usage accounting, the
  limiter and the error paths.
