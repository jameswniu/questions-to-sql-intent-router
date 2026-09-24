import time
from collections.abc import Callable


class RateLimiter:
    """A token bucket per user, held in this process. Behind several workers, each keeps its own buckets."""

    def __init__(
        self, capacity: int = 20, per_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self._rate = capacity / per_seconds
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """Spends a token and returns 0, or returns the seconds until the next token is due."""
        now = self._clock()
        tokens, then = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(float(self.capacity), tokens + (now - then) * self._rate)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self._rate
