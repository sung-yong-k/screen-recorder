"""A monotonic clock that can be paused, shared by all capture threads."""

from __future__ import annotations

import threading
import time


class Clock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t0 = None
        self._paused_at = None
        self._paused_total = 0.0

    def start(self) -> None:
        with self._lock:
            self._t0 = time.perf_counter()
            self._paused_at = None
            self._paused_total = 0.0

    def pause(self) -> None:
        with self._lock:
            if self._t0 is not None and self._paused_at is None:
                self._paused_at = time.perf_counter()

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                self._paused_total += time.perf_counter() - self._paused_at
                self._paused_at = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def elapsed(self) -> float:
        with self._lock:
            if self._t0 is None:
                return 0.0
            now = self._paused_at if self._paused_at is not None else time.perf_counter()
            return max(0.0, now - self._t0 - self._paused_total)
