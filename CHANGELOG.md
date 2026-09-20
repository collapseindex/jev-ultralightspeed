# Changelog

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
