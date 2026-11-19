"""
Deterministic sizing arithmetic for the SAC agent.

Every constant is a documented default, overridable through SizingParameters
and recorded on the artifact. None is calibrated; see docs/SIZING.md.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SizingParameters:
    """Constants used by sizing. All recorded in the output."""

    # Valkey per-key overhead: dict entry, object header, SDS headers, and
    # allocator rounding. A round figure for small string values.
    entry_overhead_bytes: int = 64
    # Key length. A query-cache key is a prefix plus a hash of query and
    # parameters; an object key is a prefix plus the key value.
    query_key_bytes: int = 48
    object_key_prefix_bytes: int = 16
    # Serialised value versus raw column widths (field names, separators, and
    # type tags of a JSON or MessagePack encoding).
    value_encoding_factor: float = 1.25
    # jemalloc fragmentation on a long-running node.
    fragmentation_factor: float = 1.10
    # Room for growth on top of today's data.
    growth_headroom: float = 0.25
    # Traffic multiplier for peak. 1.0 sizes on the statistics-window average,
    # because peak was not measured.
    peak_factor: float = 1.0
    replicas_per_shard: int = 2
    # One cache operation removes one invalidated entry or entry group.
    ops_per_invalidation: float = 1.0

    def as_dict(self) -> dict:
        return asdict(self)


def window_entries(calls: float | None, k: float | None) -> float | None:
    """Distinct entries N calls over K equally popular keys would create.

    K(1 - e^(-N/K)): the expected number of distinct keys drawn in N draws. It
    never exceeds K or N.

    Args:
        calls: Calls in the statistics window.
        k: Distinct keys possible.

    Returns:
        Expected distinct entries, or None when either input is missing.
    """
    if not calls or not k or calls <= 0 or k <= 0:
        return None
    return k * -math.expm1(-calls / k)


def bytes_per_entry(value_bytes: float, key_bytes: float,
                    p: SizingParameters) -> float:
    """Key plus encoded value plus fixed per-entry overhead."""
    return key_bytes + value_bytes * p.value_encoding_factor + p.entry_overhead_bytes


def object_key_bytes(key_widths: list[float], p: SizingParameters) -> float:
    """Prefix plus the key value, at least 8 bytes per key column."""
    return p.object_key_prefix_bytes + sum(max(w, 8.0) for w in key_widths)


def item_traffic(read_ops: float, hit_rate: float | None, write_rate: float,
                 value_bytes: float | None, p: SizingParameters) -> dict:
    """Cache operations and network throughput for one item.

    Reads are every call the cache now serves. A miss also fills the entry. A
    write to a table the item reads invalidates it. Hit rate unknown counts
    every read as a miss, which overstates writes rather than understating them.

    Returns:
        {read, fill, invalidation, total, bandwidth_gbps}, per second.
    """
    h = hit_rate if hit_rate is not None else 0.0
    fill = read_ops * (1.0 - h)
    inval = write_rate * p.ops_per_invalidation
    # Values cross the network on every hit and every fill.
    bw = (read_ops * h + fill) * (value_bytes or 0.0) * p.value_encoding_factor * 8 / 1e9
    return {"read": read_ops, "fill": fill, "invalidation": inval,
            "total": read_ops + fill + inval, "bandwidth_gbps": bw}


def memory_needed_gb(data_bytes: float, p: SizingParameters) -> float:
    """Data bytes to GB, with fragmentation and growth headroom.

    The 25% reserved-memory rule is applied later, in node selection, against
    usable node memory. Applying it here too would double-count it.
    """
    return data_bytes / 1024 ** 3 * p.fragmentation_factor * (1.0 + p.growth_headroom)
