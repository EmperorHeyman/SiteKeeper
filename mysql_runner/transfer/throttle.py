"""One speed limit shared by every connection a pool is running.

Six connections at full tilt is the right default for a deploy and the wrong
one for an office at two in the afternoon: the queue finishes fast and takes
the uplink with it, so everyone else's calls break up. A limit that applied
per connection would be no use - raising the connection count would raise the
ceiling with it - so this bucket is owned by the pool and consulted by all of
its workers, and the number the user sets is the number the link sees.

It is a plain token bucket. Tokens accrue at the configured rate up to half a
second's worth, which is what lets a burst through without letting a sustained
transfer exceed the average. A chunk larger than the whole bucket is allowed
to overdraw it rather than deadlock waiting for capacity that will never
exist; the debt is repaid before the next chunk goes out, so the average still
holds.

Qt-free and lock-guarded: the callers are pool worker threads.
"""

from __future__ import annotations

import threading
import time

#: Longest single sleep before the caller gets a turn to notice a cancel.
_MAX_SLICE = 0.2

#: Seconds of transfer the bucket may hold, so a limit does not turn every
#: chunk into a separate sleep. Half a second is small enough that the limit
#: is felt immediately and large enough to cover one chunk on any protocol.
_BURST_SECONDS = 0.5


class RateLimiter:
    """A shared byte budget. ``rate`` of 0 means no limit at all."""

    def __init__(self, rate: int = 0) -> None:
        self._lock = threading.Lock()
        self._rate = max(0, int(rate or 0))
        # Empty, not full. A bucket that starts full hands out half a second
        # of unmetered transfer before the limit begins, which on a queue of
        # small files is most of the queue: a limit of 200 KB/s measured 288
        # over the first 300 KB. The allowance is there to smooth a burst
        # once transfer is under way, not to grant one for free at the start.
        self._tokens = 0.0
        self._stamp = time.monotonic()

    @property
    def rate(self) -> int:
        """Bytes per second, or 0 when unlimited."""
        with self._lock:
            return self._rate

    @property
    def limited(self) -> bool:
        with self._lock:
            return self._rate > 0

    def set_rate(self, rate: int) -> None:
        """Change the limit. Takes effect on the next chunk, not the next file."""
        rate = max(0, int(rate or 0))
        with self._lock:
            if rate == self._rate:
                return
            self._rate = rate
            # Start the new limit from an empty-ish bucket rather than letting
            # tokens banked at the old rate spend themselves at the new one.
            self._tokens = min(self._tokens, float(rate) * _BURST_SECONDS)
            self._stamp = time.monotonic()

    def take(self, amount: int, interrupt=None) -> None:
        """Block until ``amount`` bytes may go out.

        ``interrupt`` is called before every sleep; raising from it is how a
        cancelled transfer escapes a long wait rather than finishing it first.
        """
        # Read unlocked first: this runs once per chunk on every worker, and
        # taking a lock thousands of times a second to be told there is no
        # limit is a cost paid by everyone who never set one.
        if amount <= 0 or self._rate <= 0:
            return
        while True:
            with self._lock:
                if self._rate <= 0:
                    return  # switched off, or never on
                capacity = float(self._rate) * _BURST_SECONDS
                now = time.monotonic()
                self._tokens = min(
                    capacity, self._tokens + (now - self._stamp) * self._rate
                )
                self._stamp = now
                # A chunk bigger than the bucket takes what there is and goes
                # into debt: waiting for capacity the bucket cannot hold would
                # never end.
                if self._tokens >= min(float(amount), capacity):
                    self._tokens -= amount
                    return
                wait = min((amount - self._tokens) / self._rate, _MAX_SLICE)
            if interrupt is not None:
                interrupt()
            time.sleep(max(wait, 0.001))
