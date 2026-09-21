"""
jev-ultralightspeed: classify a pile of items with Jev, fast.

The naive loop sends one item per request and waits for each answer, which
spends most of its time on round trips and re-sends the same overhead every
time. Three changes, none of them clever, together give roughly twenty times
the throughput for a little over a third of the tokens:

  pack        several items into one request, one question each
  parallel    several requests in flight, under the published rate limit
  reuse       one connection, kept open, multiplexed where possible
  dedupe      identical text asked once

The standard library alone gets you a thread per connection on HTTP/1.1. With
httpx and h2 installed (`pip install "jev-ultralightspeed[fast]"`) every
request in flight shares one HTTP/2 connection instead, which measured about
twice as fast. Either way, bring your own key.

    from jev_ultralightspeed import classify

    answers = classify(messages, "Does this need a human today?")
"""

from . import _http2, _ledger  # noqa: F401

# Re-exported so the package is the one place anything is imported from.
from ._answers import Answer, Ask, Usage, _Ask, _copy_answer, _read, triage  # noqa: F401
from ._calibrate import Calibration, calibrate, discrimination  # noqa: F401
from ._client import Client, classify, judge  # noqa: F401
from ._errors import JevError  # noqa: F401
from ._limits import _Limiter  # noqa: F401
from ._protocol import (
                        GUIDANCE,
                        _clean,
                        _guidance_text,
                        _one_body,
                        _packed_body,
                        _question,  # noqa: F401
                        _same_question,
)
from ._settings import (
                        CHARS_PER_TOKEN,
                        DRAIN_S,
                        MAX_FAILURES_KEPT,
                        MAX_ITEM_CHARS,
                        MAX_LATENCIES,
                        MAX_REQUEST_TOKENS,
                        MAX_RETRIES,
                        MAX_STATE_TOKENS,
                        MIN_SKIP_BUDGET,
                        MODEL,
                        PACK,
                        REQUESTS_PER_MINUTE,
                        RETRY_STATUSES,
                        SKIP_FRACTION,
                        SSL_CONTEXT,
                        TIMEOUT_S,
                        URL,
                        WARM_TIMEOUT_S,
                        WINDOW_PER_WORKER,  # noqa: F401
                        WORKERS,
)

__version__ = "0.22.0"

__all__ = ["Answer", "Ask", "Calibration", "Client", "JevError", "Usage",
           "calibrate", "classify", "discrimination", "judge", "triage"]
