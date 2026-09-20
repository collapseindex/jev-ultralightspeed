# Changelog

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
