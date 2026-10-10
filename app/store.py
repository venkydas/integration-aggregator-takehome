"""In-memory stores with expiry.

Everything here lives in one process and one event loop, so no locking is
needed. It is also why the service runs as a single replica; see DESIGN.md.
"""

import time
from collections.abc import Callable


class TTLStore[V]:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, tuple[float, V]] = {}

    def put(self, key: str, value: V) -> None:
        self._items[key] = (self._clock() + self._ttl, value)

    def get(self, key: str) -> V | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= self._clock():
            del self._items[key]
            return None
        return value

    def pop(self, key: str) -> V | None:
        value = self.get(key)
        self._items.pop(key, None)
        return value

    def purge(self) -> int:
        now = self._clock()
        expired = [k for k, (expires_at, _) in self._items.items() if expires_at <= now]
        for k in expired:
            del self._items[k]
        return len(expired)

    def __len__(self) -> int:
        return len(self._items)
