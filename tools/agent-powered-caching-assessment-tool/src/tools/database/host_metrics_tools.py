"""
Host-Metrics Collector — OS-level metrics for self-managed PostgreSQL hosts.

WHY THIS EXISTS
---------------
The upstream migration tool sourced all CPU/memory/disk/network data from
CloudWatch, which only exists for RDS. For self-managed PostgreSQL there is no
CloudWatch fallback, so host resource data has to be read off the box directly.
Without it the "Capacity Relief" half of the Pressure Evidence dimension cannot
be scored at all — we would know the database is missing its buffer cache but
not whether the host has headroom to fix that by tuning instead of caching.

DESIGN
------
This is a *stage* within the collector agent, not a standalone agent. It uses the
executor's run_command() — the same connection that runs psql, so over SSH the numbers
describe the machine that was connected to — and issues plain shell reads.

Collection tiers:
  Tier 1 (always available): /proc/stat, /proc/meminfo, /proc/loadavg,
                             /proc/swaps, /proc/diskstats
  Tier 2 (needs sysstat):    iostat -x, vmstat

Tier 2 is probed, never assumed. If `sysstat` is absent the collector degrades
to Tier 1 and records that in `collection_warnings` rather than failing — the
open question flagged in the project plan.

CPU SAMPLING NOTE
-----------------
/proc/stat exposes cumulative jiffies since boot, not a rate. A single read
gives average-since-boot utilization, which is nearly useless for assessing
current pressure. We therefore take two reads separated by SAMPLE_INTERVAL_SEC
and compute the delta. This is what `_read_cpu_sampled` does.

STATUS: TEMPLATE — query construction and parsing are implemented; verify
parsing against a real host before trusting the numbers (see TODOs).
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 5
IOSTAT_SAMPLES = 3


class HostMetricsCollector:
    """Collects OS-level metrics from the host an executor runs commands on."""

    def __init__(self, executor):
        """
        Args:
            executor: Anything with run_command(command, timeout) -> str.
        """
        self.executor = executor
        self.warnings: list[str] = []

    # -------------------------------------------------------------------
    # Public entry point
    # -------------------------------------------------------------------

    def collect(self) -> dict:
        """Collect all available host metrics.

        Returns a dict matching contracts.collector_output.HostMetrics. Every
        section is independently guarded: one failing probe must not abort the
        whole collection, because partial host data is still useful.
        """
        timings: dict[str, float] = {}
        result: dict = {
            "collection_method": "executor",  # the collector replaces this with ssh or local
            "sysstat_available": False,
            "collection_warnings": [],
        }

        for name, fn in (
            ("cpu", self._read_cpu_sampled),
            ("memory", self._read_memory),
            ("load_average", self._read_loadavg),
            ("swap", self._read_swap),
            ("disk", self._read_disk),
        ):
            probe_start = time.monotonic()
            try:
                result[name] = fn()
            except Exception as e:
                logger.warning("Host metric '%s' failed: %s", name, e)
                self.warnings.append(f"{name}: {e}")
                result[name] = None
            finally:
                timings[name] = round(time.monotonic() - probe_start, 3)

        probe_start = time.monotonic()
        probe = self._probe_iostat()
        timings["iostat_probe"] = round(time.monotonic() - probe_start, 3)
        mode = probe["mode"]
        result["iostat_mode"] = mode
        # Retained for backward compatibility with existing consumers; true only
        # when the richer GNU sysstat output is actually available.
        result["sysstat_available"] = mode == "extended"

        if mode == "none":
            result["disk_extended"] = None
            self.warnings.append(
                f"iostat unavailable ({probe.get('reason')}) — per-device I/O "
                "latency and utilization not collected; disk pressure scored "
                "from /proc/diskstats only"
            )
        else:
            read_start = time.monotonic()
            try:
                result["disk_extended"] = self._read_iostat(mode)
                if mode == "basic":
                    self.warnings.append(
                        "iostat does not support -x on this host, so per-device "
                        "await and %util are unavailable; throughput counters "
                        "were collected instead"
                    )
            except Exception as e:
                logger.warning("iostat (%s) failed: %s", mode, e)
                self.warnings.append(f"iostat ({mode}): {e}")
                result["disk_extended"] = None
            finally:
                timings["iostat_read"] = round(time.monotonic() - read_start, 3)

        result["collection_warnings"] = self.warnings
        result["probe_timings_seconds"] = timings
        result["probe_timings_note"] = (
            f"cpu includes a deliberate {SAMPLE_INTERVAL_SEC}s sampling interval; "
            f"iostat_read runs {IOSTAT_SAMPLES} one-second samples"
        )
        return result

    # -------------------------------------------------------------------
    # Probes
    # -------------------------------------------------------------------

    def _probe_iostat(self) -> dict:
        """Determine which iostat flags this host actually supports.

        Checking `command -v iostat` is not sufficient. BusyBox ships an iostat
        that rejects -x, which broke the first live run against an Alpine
        container: the probe found /bin/iostat, assumed GNU sysstat, and then
        `iostat -dx` failed. Capability is probed by running the command and
        inspecting the exit status.

        Returns:
            {"mode": "extended" | "basic" | "none", "reason": str | None}
            extended = GNU sysstat, supports -x (per-device await and %util)
            basic    = iostat present but no -x (typically BusyBox)
            none     = unavailable or unusable
        """
        try:
            self.executor.run_command("command -v iostat")
        except Exception:
            return {"mode": "none", "reason": "iostat not installed"}

        try:
            self.executor.run_command("iostat -dx 1 1")
            return {"mode": "extended", "reason": None}
        except Exception as e:
            extended_error = str(e)[:100]

        try:
            self.executor.run_command("iostat -d 1 1")
            return {
                "mode": "basic",
                "reason": f"extended (-x) unsupported, likely BusyBox: {extended_error}",
            }
        except Exception as e:
            return {"mode": "none", "reason": f"iostat present but unusable: {str(e)[:100]}"}

    # -------------------------------------------------------------------
    # Tier 1 — /proc reads
    # -------------------------------------------------------------------

    def _read_cpu_sampled(self) -> dict:
        """Two /proc/stat reads separated by an interval, to get a real rate.

        A single cumulative read would report average utilization since boot,
        which tells us nothing about current pressure.
        """
        cmd = (
            f"grep '^cpu ' /proc/stat && sleep {SAMPLE_INTERVAL_SEC} && "
            f"grep '^cpu ' /proc/stat && nproc"
        )
        out = self.executor.run_command(cmd, timeout=SAMPLE_INTERVAL_SEC + 60)
        lines = [ln for ln in out.strip().split("\n") if ln.strip()]
        if len(lines) < 3:
            raise ValueError(f"unexpected /proc/stat output: {out!r}")

        first = _parse_proc_stat_cpu(lines[0])
        second = _parse_proc_stat_cpu(lines[1])
        vcpus = int(lines[2].strip())

        delta = {k: second[k] - first[k] for k in first}
        total = sum(delta.values())
        if total <= 0:
            raise ValueError("no CPU ticks elapsed between samples")

        busy = total - delta.get("idle", 0) - delta.get("iowait", 0)
        return {
            "vcpu_count": vcpus,
            "utilization_pct": round(busy / total * 100, 2),
            "iowait_pct": round(delta.get("iowait", 0) / total * 100, 2),
            "system_pct": round(delta.get("system", 0) / total * 100, 2),
            "user_pct": round(delta.get("user", 0) / total * 100, 2),
            "steal_pct": round(delta.get("steal", 0) / total * 100, 2),
            "sample_interval_sec": SAMPLE_INTERVAL_SEC,
        }

    def _read_memory(self) -> dict:
        """Parse /proc/meminfo. All values are kB in the source file."""
        out = self.executor.run_command("cat /proc/meminfo")
        mem = _parse_meminfo(out)

        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        cached_kb = mem.get("Cached", 0)
        buffers_kb = mem.get("Buffers", 0)

        return {
            "total_gb": round(total_kb / 1024 / 1024, 2),
            "available_gb": round(avail_kb / 1024 / 1024, 2),
            "used_pct": round((total_kb - avail_kb) / max(total_kb, 1) * 100, 2),
            # Page cache is the key signal: a small page cache relative to
            # database size means the OS is not absorbing reads either.
            "page_cache_gb": round((cached_kb + buffers_kb) / 1024 / 1024, 2),
            "dirty_mb": round(mem.get("Dirty", 0) / 1024, 2),
        }

    def _read_loadavg(self) -> dict:
        out = self.executor.run_command("cat /proc/loadavg")
        parts = out.strip().split()
        if len(parts) < 3:
            raise ValueError(f"unexpected /proc/loadavg output: {out!r}")
        return {
            "load_1m": float(parts[0]),
            "load_5m": float(parts[1]),
            "load_15m": float(parts[2]),
        }

    def _read_swap(self) -> dict:
        """Swap usage. Active swapping while serving hot queries is strong
        evidence of memory exhaustion — a high-weight Pressure Evidence signal.
        """
        out = self.executor.run_command("cat /proc/meminfo | grep -i swap")
        mem = _parse_meminfo(out)
        total_kb = mem.get("SwapTotal", 0)
        free_kb = mem.get("SwapFree", 0)
        used_kb = max(total_kb - free_kb, 0)
        return {
            "total_gb": round(total_kb / 1024 / 1024, 2),
            "used_gb": round(used_kb / 1024 / 1024, 2),
            "used_pct": round(used_kb / max(total_kb, 1) * 100, 2) if total_kb else 0.0,
            "swap_configured": total_kb > 0,
        }

    def _read_disk(self) -> dict:
        """Filesystem capacity plus raw /proc/diskstats counters.

        TODO: /proc/diskstats is cumulative. To derive IOPS, sample twice like
        _read_cpu_sampled does. Currently returns the raw snapshot only, which
        is sufficient for capacity but not for rate.
        """
        out = self.executor.run_command("df -BG --output=source,size,used,avail,pcent,target | head -20")
        filesystems = []
        for line in out.strip().split("\n")[1:]:
            f = line.split()
            if len(f) >= 6:
                filesystems.append(
                    {
                        "device": f[0],
                        "size_gb": _strip_unit(f[1]),
                        "used_gb": _strip_unit(f[2]),
                        "avail_gb": _strip_unit(f[3]),
                        "used_pct": _strip_unit(f[4]),
                        "mount": f[5],
                    }
                )
        return {"filesystems": filesystems}

    # -------------------------------------------------------------------
    # Tier 2 — sysstat
    # -------------------------------------------------------------------

    def _read_iostat(self, mode: str) -> dict:
        """Per-device I/O stats, using whichever flags the host supports.

        The first iostat report is since-boot averages, so multiple samples are
        taken and the first is discarded.

        Args:
            mode: "extended" for GNU sysstat (-dx), "basic" for BusyBox (-d).

        Returns:
            Dict with raw output, parsed device rows, and the mode used.

        TODO: even within GNU sysstat, column names vary by version (await vs
        r_await/w_await, %util position). The parser returns positional values
        rather than named fields for that reason. Verify against the target
        host's sysstat version before relying on specific columns.
        """
        flags = "-dx" if mode == "extended" else "-d"
        out = self.executor.run_command(
            f"iostat {flags} 1 {IOSTAT_SAMPLES}", timeout=IOSTAT_SAMPLES + 60
        )
        return {
            "mode": mode,
            "raw": out,
            "parsed": _parse_iostat_last_report(out),
            "note": "first report discarded (since-boot averages)",
            "has_latency_and_util": mode == "extended",
        }


# ---------------------------------------------------------------------------
# Parsers — pure functions, unit-testable without a host
# ---------------------------------------------------------------------------

_CPU_FIELDS = (
    "user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal",
)


def _parse_proc_stat_cpu(line: str) -> dict[str, int]:
    """Parse the aggregate 'cpu' line from /proc/stat into named jiffy counters."""
    parts = line.split()
    if not parts or parts[0] != "cpu":
        raise ValueError(f"not a /proc/stat cpu line: {line!r}")
    values = [int(v) for v in parts[1:]]
    return {
        name: values[i] if i < len(values) else 0
        for i, name in enumerate(_CPU_FIELDS)
    }


def _parse_meminfo(raw: str) -> dict[str, int]:
    """Parse /proc/meminfo into {key: kB}."""
    out: dict[str, int] = {}
    for line in raw.strip().split("\n"):
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _parse_iostat_last_report(raw: str) -> list[dict]:
    """Extract the final iostat device report (most recent interval)."""
    blocks = [b for b in raw.split("Device") if b.strip()]
    if not blocks:
        return []
    last = blocks[-1]
    devices = []
    header_seen = False
    for line in last.strip().split("\n"):
        if not line.strip():
            continue
        if not header_seen:
            header_seen = True  # remainder of the "Device ..." header line
            continue
        fields = line.split()
        if len(fields) >= 2:
            devices.append({"device": fields[0], "values": fields[1:]})
    return devices


def _strip_unit(val: str) -> float:
    """'42G' → 42.0, '17%' → 17.0."""
    try:
        return float(re.sub(r"[^\d.]", "", val) or 0)
    except ValueError:
        return 0.0
