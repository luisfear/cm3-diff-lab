"""progress.py -- lightweight progress reporting for long runs.

Prints a throttled one-line status to stdout (at most once every few seconds).
Public logs must stay free of identifiers, so the detail string is the caller's
responsibility; pass counters, not names.
"""

from __future__ import annotations

import sys
import time


class Progress:
    def __init__(self, name: str, total: int, description: str = ""):
        self.name = name
        self.total = max(1, total)
        self.description = description
        self.t0 = time.time()
        self._done = 0
        self._last = 0.0
        self._last_pct = -1

    def step(self, done: int, detail: str = ""):
        self._done = done
        pct = int(100 * done / self.total)
        now = time.time()
        if pct != self._last_pct or now - self._last > 5:
            self._last_pct = pct
            self._last = now
            print(f"[{self.name}] {pct:3d}% ({done}/{self.total}) {detail}", flush=True)

    def done(self, summary: str = ""):
        dt = time.time() - self.t0
        print(f"[{self.name}] done in {dt:.0f}s {summary}", flush=True)
