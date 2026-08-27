"""Rate limiting (PD-07).

Two exposures made this necessary, both created by the customer-facing shift
(D7) that opened enrollment and capture-check to unauthenticated callers:

* ``/v1/pay`` accepts a pay code per attempt. That code is one of the two SCA
  factors (D2), and a short secret with unlimited attempts is enumerable.
* ``/v1/capture/check`` and ``/v1/enroll`` run segmentation, Gabor filtering
  and an FFT per call, at no cost to the caller.

Fixed windows, not a sliding log
--------------------------------
A fixed window admits up to 2x the limit across a window boundary. That is a
known and accepted imprecision here: the purpose is to stop enumeration and
resource exhaustion, not to meter an API quota to the request. The alternative
costs a row per request and buys nothing this system needs.

Counters live in the database rather than in process memory, for the same
reason as the low-value counters: a limiter that forgets on restart, or that
the next server behind the load balancer cannot see, is not a limiter.

Failing closed
--------------
When a limit is hit the request is refused, including for otherwise valid
credentials. That is deliberate for the payment path -- an attacker who has
learned a pay code should not be able to grind against it -- and it is why
lockouts are audited, so a customer complaining they cannot pay is diagnosable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..store.repository import Repository


class RateLimited(Exception):
    """The caller exceeded a limit. ``retry_after`` is in seconds."""

    def __init__(self, scope: str, retry_after: int) -> None:
        super().__init__(f"too many {scope} attempts; retry in {retry_after}s")
        self.scope = scope
        self.retry_after = retry_after


@dataclass(frozen=True)
class Limit:
    """A limit of ``max_hits`` per ``window_seconds``."""

    max_hits: int
    window_seconds: int


# Chosen to be invisible to real use and obstructive to abuse.
#
# pay: a customer paying repeatedly at one till is normal; hundreds of attempts
# against one pay code is not.
# enroll / capture: enrollment happens once per customer, and framing a hand
# needs a handful of checks, not hundreds.
DEFAULT_LIMITS: dict[str, Limit] = {
    "pay_by_hint": Limit(max_hits=10, window_seconds=300),
    "pay_by_terminal": Limit(max_hits=120, window_seconds=60),
    "enroll": Limit(max_hits=5, window_seconds=3600),
    "capture_check": Limit(max_hits=60, window_seconds=60),
}


@dataclass
class RateLimiter:
    repository: Repository
    limits: dict[str, Limit] | None = None

    def _limit_for(self, scope: str) -> Limit:
        limits = self.limits or DEFAULT_LIMITS
        return limits[scope]

    def check(self, scope: str, identity: str, now: datetime | None = None) -> None:
        """Count an attempt and raise ``RateLimited`` if over the limit.

        ``identity`` is whatever should be limited independently -- a hashed
        pay code, a terminal id, a client address. It is never a raw secret:
        callers pass an already-hashed value so the limiter table cannot become
        a directory of pay codes.
        """
        limit = self._limit_for(scope)
        moment = now or datetime.now(timezone.utc)

        # Store the window's start as epoch seconds, not as a window index.
        # Indices are only comparable within one window length, and scopes here
        # use different lengths -- so an index-based purge would compare
        # 5-minute buckets against 1-hour buckets and delete live windows.
        epoch = int(moment.timestamp())
        window_start = str(epoch - (epoch % limit.window_seconds))
        bucket = f"{scope}:{identity}"

        if self.repository.hit_rate_limit(bucket, window_start, limit.max_hits):
            elapsed = epoch % limit.window_seconds
            raise RateLimited(scope, retry_after=limit.window_seconds - elapsed)

    def purge_expired(self, now: datetime | None = None) -> int:
        """Drop windows old enough that no limit could still reference them."""
        moment = now or datetime.now(timezone.utc)
        longest = max((self.limits or DEFAULT_LIMITS).values(), key=lambda l: l.window_seconds)
        cutoff = moment - timedelta(seconds=longest.window_seconds * 2)
        return self.repository.purge_rate_limits(str(int(cutoff.timestamp())))
