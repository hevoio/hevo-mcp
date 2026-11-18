"""Per-caller rate limiting, applied before a call reaches Hevo.

One token bucket per (caller, reads|writes) in this process. Replicas don't share state, so with N
replicas behind a load balancer a caller's effective limit is up to N times the configured one; the
limit is a brake on runaway agents, not a quota. Hevo's own limits still apply and surface as
UPSTREAM_RATE_LIMITED.
"""

import math
import time
from dataclasses import dataclass

IDLE_SECONDS = 600  # buckets untouched this long are dropped on the next sweep
SWEEP_EVERY = 1000  # calls between sweeps


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, reads_per_minute: int, writes_per_minute: int, clock=time.monotonic):
        self.rates = {"read": reads_per_minute, "write": writes_per_minute}
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._calls = 0

    def capacity(self, is_write: bool) -> int | None:
        """The most tokens one call can ever take (a full bucket), or None when there is no limit. A batch with
        more targets than this can never be allowed, however long the caller waits."""
        limit = self.rates["write" if is_write else "read"]
        return limit if limit > 0 else None

    def check(self, caller: str, is_write: bool, tokens: int = 1, spend: bool = True) -> int | None:
        """Takes `tokens` tokens (one per request that would reach Hevo); returns None if allowed, else whole
        seconds until enough tokens are back. With `spend` false it only looks: the answer is the same, but
        the tokens stay (used before an approval prompt, which must not cost tokens the answered call then
        pays again)."""
        kind = "write" if is_write else "read"
        limit = self.rates[kind]
        if limit <= 0:
            return None
        now = self._clock()
        self._calls += 1
        if self._calls % SWEEP_EVERY == 0:
            self._sweep(now)
        per_second = limit / 60.0
        bucket = self._buckets.get((caller, kind))
        if bucket is None:
            bucket = self._buckets[(caller, kind)] = _Bucket(tokens=float(limit), updated=now)
        else:
            bucket.tokens = min(float(limit), bucket.tokens + (now - bucket.updated) * per_second)
            bucket.updated = now
        need = float(max(1, tokens))
        if bucket.tokens >= need:
            if spend:
                bucket.tokens -= need
            return None
        return max(1, math.ceil((min(need, float(limit)) - bucket.tokens) / per_second))

    def _sweep(self, now: float) -> None:
        stale = [k for k, b in self._buckets.items() if now - b.updated > IDLE_SECONDS]
        for k in stale:
            del self._buckets[k]
