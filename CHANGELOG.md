# Changelog

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
