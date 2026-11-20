"""Put the directory holding `ecdl_link/` on sys.path. Import for the side effect.

`ecdl_link` lives at the repo root so the CLI and the API can share it. Two
layouts have to work: the container has `ecdl_link/` beside this file (mounted
or copied into /app), and a local run has the repo root one level up.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_candidate, 'ecdl_link')):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break
