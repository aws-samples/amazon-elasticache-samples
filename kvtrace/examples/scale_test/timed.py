#!/usr/bin/env python3
"""Run a command and report its wall time and peak RSS (children included)."""

import resource
import subprocess
import sys
import time

t0 = time.monotonic()
rc = subprocess.call(sys.argv[1:])
wall = time.monotonic() - t0
peak_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(f"wall_seconds={wall:.1f} peak_rss_mib={peak_kib / 1024:.0f} rc={rc}", file=sys.stderr)
sys.exit(rc)
