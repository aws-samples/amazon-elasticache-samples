"""Resource usage of the recorder process itself (CPU and RSS), standard library only."""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, Optional

try:
    import resource
except ImportError:  # not available on Windows
    resource = None  # type: ignore[assignment]


def current_rss_bytes() -> Optional[int]:
    """Resident set size now, from /proc/self/status (Linux); None elsewhere."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def max_rss_bytes() -> Optional[int]:
    """Peak resident set size of this process."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS reports bytes.
    return int(peak if sys.platform == "darwin" else peak * 1024)


class ProcessSampler:
    """CPU% of this process between two calls of ``sample``, plus RSS.

    CPU% is user+system time over wall time, so a process that keeps more than one
    thread busy can report more than 100.
    """

    def __init__(self, wall: Callable[[], float] = time.monotonic,
                 cpu: Callable[[], float] = lambda: sum(os.times()[:2])):
        self._wall = wall
        self._cpu = cpu
        self._started_wall = self._last_wall = wall()
        self._started_cpu = self._last_cpu = cpu()

    def sample(self) -> Dict[str, Optional[float]]:
        wall, cpu = self._wall(), self._cpu()
        span = wall - self._last_wall
        total_span = wall - self._started_wall
        out: Dict[str, Optional[float]] = {
            "cpu_pct": round(100.0 * (cpu - self._last_cpu) / span, 2) if span > 0 else None,
            "cpu_pct_avg": (round(100.0 * (cpu - self._started_cpu) / total_span, 2)
                            if total_span > 0 else None),
            "cpu_seconds": round(cpu - self._started_cpu, 3),
            "rss_bytes": current_rss_bytes(),
            "max_rss_bytes": max_rss_bytes(),
        }
        self._last_wall, self._last_cpu = wall, cpu
        return out
