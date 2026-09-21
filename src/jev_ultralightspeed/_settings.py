"""Every number that can be tuned, and the ones the provider fixes."""

from __future__ import annotations

import ssl

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Measured, not guessed, and remeasured against the objective that matters.
# bench_tuning.py prices eight shapes by trusted items a second, which is
# throughput times the share of verdicts you can keep at a quality bar. At a 97%
# bar, pack=32 delivers 409 of them a second against pack=8's 100, coverage
# barely moves with depth (77% against 75%), and no position effect is detectable
# at any depth. Four requests in flight keeps latency flat and the request ceiling
# binds long before more of them would help.
PACK = 32
WORKERS = 4

# TypeSafe publishes 1,200 requests a minute. Stay under it by default.
REQUESTS_PER_MINUTE = 1_000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRIES = 5
TIMEOUT_S = 60.0
MAX_ITEM_CHARS = 20_000
WARM_TIMEOUT_S = 5.0                   # warming is an optimisation, never a wait worth minutes
# `ssl.SSLContext` can be replaced at run time: pip's vendored truststore swaps in
# a subclass that defers to the operating system, and then isinstance against the
# module attribute misses a plain stdlib context. The base class is what to check.
SSL_CONTEXT = next(cls for cls in ssl.SSLContext.__mro__ if cls.__module__ == "ssl")
MAX_LATENCIES = 10_000                 # kept for percentiles, not forever
# The provider documents 64k tokens in a request and 32k for the state plus the
# longest question, and says the limits can change. Held well under both, because
# `pack` counts items and an oversized request is refused whatever the count.
MAX_REQUEST_TOKENS = 56_000
MAX_STATE_TOKENS = 28_000
CHARS_PER_TOKEN = 3.5                  # conservative: 3.92 measured on a live packed request
SKIP_FRACTION = 0.01                   # of the items in a call, before skipping gives up
MIN_SKIP_BUDGET = 5                    # requests' worth, so a small call is not held to 1%
MAX_FAILURES_KEPT = 1_000              # reported in full; the rest are counted only
_CLIENT_ARGUMENTS = ("key", "url", "model", "pack", "workers", "requests_per_minute",
                     "cache", "dedupe", "transport", "verify", "guidance", "paced",
                     "limiter", "chars_per_token")
WINDOW_PER_WORKER = 2                  # requests queued per worker, so a straggler is not a wall
DRAIN_S = 5.0                          # how long an abandoned run waits for what is still in the air
