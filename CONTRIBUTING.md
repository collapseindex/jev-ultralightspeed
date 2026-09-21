# Contributing

Issues and pull requests are welcome. The project is small on purpose and the bar is about evidence
rather than ceremony.

## Running it

```bash
git clone https://github.com/collapseindex/jev-ultralightspeed.git
cd jev-ultralightspeed
pip install pytest
python -m pytest tests -q          # a local server, no key and no network needed
```

The tests replace the one method that talks to the API, so packing, ordering, deduplication, the
cache, usage accounting, the limiter and the error paths are all covered offline. A change that
cannot be tested that way needs a note in the pull request saying why.

For the benchmarks you need a key and a few cents:

```bash
TYPESAFE_API_KEY=... python bench/sweep.py --items 256 --rounds 3
TYPESAFE_API_KEY=... python bench/soak.py --items 10000
```

## The rules that matter

1. **No required dependencies.** The standard library path has to keep working. Anything else is an
   optional extra, checked for at import and refused clearly when missing, the way `[fast]` is.
2. **Never log, print or raise the key.** Errors carry the status and the API's message. If a change
   could put a header in a traceback, it does not go in.
3. **A performance claim needs a measurement.** Not an opinion about how HTTP works. Add a row to
   `bench/sweep.py`, run it, and put the number in the pull request. If the number is small, say so; a
   change that turns out not to matter is still useful to have measured.
4. **A faster client that changes the answers is a bug.** Every benchmark reports agreement against
   a baseline for that reason. Keep it.

## Measuring honestly

The benchmarks follow four habits, and a pull request that changes them should say why:

- **warm up first**, because a cold TLS handshake is not the thing being measured
- **randomise the order of the shapes**, so drift at the provider cannot favour one of them
- **report p50 and p95**, because throughput can rise while the tail falls apart
- **compare the answers, not only the clock**, against a baseline run of the same items

If you quote a cost, quote it against the account balance afterwards. Every dollar figure in the
README was checked that way.

## Commits and style

- One change per pull request, with the measurement that justifies it.
- Conventional commit subjects: `feat:`, `fix:`, `perf:`, `docs:`, `test:`, `chore:`.
- Comments explain why, not what. The code says what.
- No em dashes in prose, and no line over 100 characters.

## What is likely to be accepted

- A transport that measures faster, behind a flag, with the numbers.
- Better handling of a failure mode that is currently ugly: a partial answer, a rate limit storm,
  a connection that dies mid-batch.
- An adaptive pack size, if it can be shown to beat a fixed 8 or 32 on a real corpus.
- Bug reports with an item that reproduces them.

## What is not

- A dependency for something the standard library already does.
- An abstraction layer over providers. This is a client for one API, deliberately.
- Anything that sends data anywhere other than the configured endpoint.
- An eval framework. That is [dinostomp](https://github.com/collapseindex/dinostomp), and it is a
  different job.

## Licence

By contributing you agree your work is licensed under [Apache-2.0](LICENSE), like the rest.
