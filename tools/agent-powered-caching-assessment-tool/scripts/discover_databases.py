"""
List PostgreSQL servers the tool could assess: on this machine, and on a database host over SSH.

Reads no password and makes no database connection. The host probe lists clusters only.

  .venv/bin/python scripts/discover_databases.py
  .venv/bin/python scripts/discover_databases.py --host ec2-user@203.0.113.10 \
      --identity-file apcat_key --artifact-root /tmp/apcat-e2e/api --json

--identity-file is a key name inside SSH_KEY_DIR; the host key must be in SSH_KNOWN_HOSTS
(or ~/.ssh/known_hosts). Spec: .kiro/specs/database-discovery.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contracts.discovery import LOCAL_SOURCES, DiscoveryResult  # noqa: E402
from src.tools.discovery.discover import HostProbeTarget, discover, host_problems  # noqa: E402


def as_text(result: DiscoveryResult) -> str:
    """A short plain-text listing of a result."""
    lines = []
    for s in result.servers:
        lines.append(f"[{s.source}] {s.label}")
        for n in s.notes:
            lines.append(f"    {n}")
    lines.append("")
    for r in result.sources:
        lines.append(f"{r.source:<20} {r.status:<12} found {r.found}" + (f"  {r.detail}" if r.detail else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run discovery, print the result.

    Returns:
        0 when every requested source ran (ok, unavailable or skipped), 2 when one failed,
        3 when an argument is not in an accepted form.
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", help="SSH target to probe for PostgreSQL clusters")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--identity-file", help="Key file name inside SSH_KEY_DIR")
    ap.add_argument("--artifact-root", help="Artifact folder, for earlier assessments")
    ap.add_argument("--sources", default=",".join(LOCAL_SOURCES),
                    help=f"Comma-separated local sources (default: {','.join(LOCAL_SOURCES)})")
    ap.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = ap.parse_args(argv)
    sources = [s for s in args.sources.split(",") if s]
    unknown = [s for s in sources if s not in LOCAL_SOURCES]
    if unknown:
        print(f"error: unknown source(s): {', '.join(unknown)}", file=sys.stderr)
        return 3
    host = None
    if args.host:
        host = HostProbeTarget(args.host, args.ssh_port, args.identity_file)
        problems = host_problems(host)
        if problems:
            print("error: " + "; ".join(problems), file=sys.stderr)
            return 3
    result = discover(sources=sources, host=host, artifact_root=args.artifact_root)  # type: ignore[arg-type]
    print(json.dumps(result.model_dump(), indent=2) if args.json else as_text(result))
    return 2 if any(r.status == "error" for r in result.sources) else 0


if __name__ == "__main__":
    sys.exit(main())
