"""Dropping stale entries from the small in-process dictionaries that stand in for Redis.

Without Redis (or while it is down) the rate limiters, the login throttle and the "last used" write
throttle keep their counters in a dictionary of this process, keyed by values a visitor chooses (an
address, an e-mail, an API-key id). Such a dictionary must not grow without limit, and the way it was
kept small cost more than the counting: past 4096 keys EVERY call rebuilt the whole dictionary, so a
request cost 486 us with 5,000 keys and 2,480 us with 50,000 (measured), on the event loop, exactly
while Redis was away and the traffic was heaviest.

`Pruner.prune()` makes that cost independent of the number of keys:

  * nothing happens below `threshold` keys;
  * above it, the stale entries are dropped at most once per `interval` seconds (in place), so the
    O(n) pass is amortised over every request of that second;
  * if the live entries alone exceed `hard_cap` (a flood of distinct keys that are all still inside
    their window), the oldest-inserted ones are dropped as well. A dropped counter simply starts again
    from zero, which weakens a limit a little for that one key and keeps memory bounded, the right
    trade while a flood is under way.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from typing import Any


class Pruner:
    def __init__(self, *, threshold: int = 4096, interval: float = 1.0, hard_cap: int = 100_000) -> None:
        if hard_cap < threshold:
            raise ValueError("hard_cap must not be below threshold")
        self.threshold = threshold
        self.interval = interval
        self.hard_cap = hard_cap
        self._next_pass = 0.0
        #: entries dropped as stale, and entries dropped only to respect hard_cap (for tests and metrics)
        self.stale_dropped = 0
        self.evicted = 0

    def prune(self, mapping: dict, is_stale: Callable[[Any], bool], now: float | None = None) -> bool:
        """Drop stale entries from `mapping` if it is big enough and it is time; True when a pass ran.
        `is_stale(value)` says whether an entry's window is over."""
        if len(mapping) <= self.threshold:
            return False
        now = time.monotonic() if now is None else now
        if now < self._next_pass:
            return False
        self._next_pass = now + self.interval
        stale = [key for key, value in mapping.items() if is_stale(value)]
        for key in stale:
            del mapping[key]
        self.stale_dropped += len(stale)
        excess = len(mapping) - self.hard_cap
        if excess > 0:
            for key in list(itertools.islice(mapping, excess)):
                del mapping[key]
            self.evicted += excess
        return True
