"""Minimal progress reporter interface retained by the transition extraction."""

from __future__ import annotations

import time
from typing import TextIO


class HeartbeatReporter:
    def __init__(self, stream: TextIO, *, interval_seconds: float = 30.0) -> None:
        self.stream = stream
        self.interval_seconds = interval_seconds
        self._last = 0.0

    def step(self, scope: str, message: str) -> None:
        print(f"[{scope}] {message}", file=self.stream)
        self._last = time.monotonic()

    def poll(self) -> None:
        now = time.monotonic()
        if now - self._last >= self.interval_seconds:
            print("[working]", file=self.stream)
            self._last = now
