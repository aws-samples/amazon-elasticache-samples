#!/usr/bin/env python3
"""Seed an ElastiCache / Valkey target with strings, hashes, and sets.

*** SANDBOX / TEST USE ONLY -- DO NOT RUN AGAINST PRODUCTION ***

This writes millions of keys and consumes GiB of memory on the target.
Against a production cluster this can evict real application data or hit
the maxmemory limit and cause write failures. Keys are namespaced
(str:, hash:, set:) but any existing keys with those prefixes will be
silently overwritten. There is no dry-run and no undo.

Auto-detects cluster-mode enabled vs disabled and uses the matching glide
client. Uses RESP2 so it works against Redis 5.0.6 (no HELLO command).
Keys are spread across all 16384 slots via hashtags so data lands on
every shard of a cluster-mode target.

Usage:
    python3 seed_data.py --host <endpoint> --gb 5
    python3 seed_data.py --host <endpoint> --gb 5 --no-tls
    python3 seed_data.py --host <endpoint> --gb 5 --password <pw>
"""

import argparse
import asyncio
import logging
import os
import random
import string
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from glide import (
    Batch,
    ClusterBatch,
    GlideClient,
    GlideClientConfiguration,
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
    ProtocolVersion,
    ServerCredentials,
)

TOTAL_SLOTS = 16384
KEY_PREFIX = "glidetest"  # namespace every key under this to avoid colliding with app data
BATCH_SIZE = 1000                # keys per pipelined batch
INFLIGHT_BATCHES = 8             # concurrent batches in flight
FLUSH_MAX_RETRIES = 3            # retry a batch this many times on TimeoutError
BATCH_REPORT_SECS = 10

# per-key sizing
STRING_VALUE_BYTES = 1024        # 1 KiB per string
HASH_FIELDS = 20                 # 20 fields x ~100B = ~2 KiB per hash
HASH_FIELD_BYTES = 100
SET_MEMBERS = 20                 # 20 members x ~100B = ~2 KiB per set
SET_MEMBER_BYTES = 100

# rough logical bytes contributed by one key of each type
BYTES_PER_STRING = STRING_VALUE_BYTES
BYTES_PER_HASH = HASH_FIELDS * HASH_FIELD_BYTES
BYTES_PER_SET = SET_MEMBERS * SET_MEMBER_BYTES

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_logfile = os.path.join(
    LOG_DIR, f"seed_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
)

logger = logging.getLogger("seed_data")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)
_fh = logging.FileHandler(_logfile)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_ALPHABET = string.ascii_letters + string.digits


def rand_str(n: int) -> str:
    """Return a random alphanumeric string of length n."""
    return "".join(random.choices(_ALPHABET, k=n))


def plan_counts(target_gb: float) -> Dict[str, int]:
    """Compute how many keys of each type to write for a target size.

    Splits the target roughly evenly by bytes across the three types.

    Args:
        target_gb: Desired logical data volume in GiB.

    Returns:
        Dict with 'strings', 'hashes', 'sets' key counts.
    """
    target_bytes = int(target_gb * (1024 ** 3))
    per_type = target_bytes // 3
    return {
        "strings": per_type // BYTES_PER_STRING,
        "hashes": per_type // BYTES_PER_HASH,
        "sets": per_type // BYTES_PER_SET,
    }


GlideAnyClient = Union[GlideClient, GlideClusterClient]
GlideAnyBatch = Union[Batch, ClusterBatch]


async def connect_auto(
    host: str,
    port: int,
    use_tls: bool,
    credentials: Optional[ServerCredentials] = None,
) -> Tuple[GlideAnyClient, bool]:
    """Connect using the correct client class for cluster-mode enabled vs disabled.

    GlideClusterClient refuses to connect at all to a cluster-mode disabled
    target, raising "No topology views found" -- confirmed empirically
    against a real standalone valkey-server. This tries the cluster client
    first, and falls back to the standalone GlideClient only when that
    specific error occurs (any other connection failure is re-raised, so
    real network/auth/TLS problems are not masked).

    Args:
        host: Cluster configuration endpoint or standalone server address.
        port: Server port.
        use_tls: Whether to connect over TLS.
        credentials: Optional AUTH credentials (password, optional username
            for ACL/RBAC). Omit for clusters with no auth configured.

    Returns:
        Tuple of (client, is_cluster_mode).

    Raises:
        Exception: Re-raises the original connection error if it is not
            the specific cluster-mode-disabled signature.
    """
    try:
        cfg = GlideClusterClientConfiguration(
            addresses=[NodeAddress(host=host, port=port)],
            use_tls=use_tls,
            protocol=ProtocolVersion.RESP2,
            inflight_requests_limit=5000,
            credentials=credentials,
        )
        client = await GlideClusterClient.create(cfg)
        return client, True
    except Exception as exc:  # noqa: BLE001
        if "No topology views found" not in str(exc):
            raise
        logger.info(
            "target is cluster-mode disabled (no topology found), "
            "falling back to standalone client"
        )

    cfg = GlideClientConfiguration(
        addresses=[NodeAddress(host=host, port=port)],
        use_tls=use_tls,
        protocol=ProtocolVersion.RESP2,
        credentials=credentials,
    )
    client = await GlideClient.create(cfg)
    return client, False


async def warm_up(
    client: GlideAnyClient,
    is_cluster: bool,
    probe_keys: int = 200,
    max_attempts: int = 10,
) -> None:
    """Wait until the target is actually ready to accept writes.

    On cluster-mode, glide's cluster client can return from create() before
    every per-shard connection is fully established. Sending a real write
    burst immediately can hit not-yet-ready shards and fail with
    TimeoutError. Confirmed empirically: a fixed 1.5s sleep was NOT reliably
    sufficient (~1 in 5 runs still failed); this retries until a real probe
    batch succeeds completely, which is deterministic against actual
    readiness rather than a guessed delay.

    Args:
        client: Connected glide client (cluster or standalone).
        is_cluster: True if client is a GlideClusterClient.
        probe_keys: Number of probe keys to write per attempt.
        max_attempts: Give up after this many attempts (each ~0.3s apart).

    Raises:
        RuntimeError: If the target never becomes ready within max_attempts.
    """
    batch_cls = ClusterBatch if is_cluster else Batch
    for attempt in range(1, max_attempts + 1):
        probe: GlideAnyBatch = batch_cls(is_atomic=False)
        for i in range(probe_keys):
            probe.set(f"{KEY_PREFIX}:warmup:{{{i % TOTAL_SLOTS}}}:{i}", "x")
        try:
            results = await client.exec(probe, raise_on_error=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("warm_up attempt %d raised: %r", attempt, exc)
            results = None

        ok_count = 0
        if results:
            ok_count = sum(1 for r in results if r in (b"OK", "OK"))

        if ok_count == probe_keys:
            if attempt > 1:
                logger.info("target ready after %d warm-up attempt(s)", attempt)
            # Remove the probe keys so they don't linger in the target.
            try:
                await client.delete(
                    [f"{KEY_PREFIX}:warmup:{{{i % TOTAL_SLOTS}}}:{i}" for i in range(probe_keys)]
                )
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                logger.debug("warm-up key cleanup failed (harmless): %r", exc)
            return

        logger.debug(
            "warm_up attempt %d: %d/%d probe writes ok, retrying",
            attempt, ok_count, probe_keys,
        )
        await asyncio.sleep(0.3)

    raise RuntimeError(
        f"target did not become ready after {max_attempts} warm-up attempts"
    )


async def seed(
    client: GlideAnyClient, is_cluster: bool, counts: Dict[str, int]
) -> Dict[str, int]:
    """Write the planned keys using pipelined non-atomic batches.

    Batching is ~2.5x faster than per-operation awaits because it amortizes
    network round-trips. Non-atomic batches may span hash slots on cluster
    targets; on standalone targets there is only one node so slot hashtags
    in key names have no routing effect (harmless -- keys still land
    correctly, just without the multi-shard distribution).

    Args:
        client: Connected glide client (cluster or standalone).
        is_cluster: True if client is a GlideClusterClient.
        counts: Output of plan_counts().

    Returns:
        Dict of written counts per type, plus 'errors'.
    """
    batch_cls = ClusterBatch if is_cluster else Batch
    written = {"strings": 0, "hashes": 0, "sets": 0, "errors": 0}
    start = time.time()
    last_report = start

    total_ops = counts["strings"] + counts["hashes"] + counts["sets"]
    max_n = max(counts.values())
    done = 0
    sem = asyncio.Semaphore(INFLIGHT_BATCHES)

    async def flush(batch: GlideAnyBatch, tally: Dict[str, int]) -> None:
        """Execute one batch and record success/failure counts.

        Retries on timeout. Glide's cluster client intermittently raises
        TimeoutError under sustained concurrent load against this cluster
        (confirmed empirically: reproducible even after a readiness probe
        succeeds, so it is not purely a cold-start issue). A short retry
        clears it in practice; only count as a real error after exhausting
        retries.
        """
        async with sem:
            last_exc: Exception = None
            for attempt in range(1, FLUSH_MAX_RETRIES + 1):
                try:
                    await client.exec(batch, raise_on_error=False)
                    for k, v in tally.items():
                        written[k] += v
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < FLUSH_MAX_RETRIES:
                        logger.debug(
                            "batch exec failed (attempt %d/%d): %r, retrying",
                            attempt, FLUSH_MAX_RETRIES, exc,
                        )
                        await asyncio.sleep(0.2 * attempt)
            written["errors"] += sum(tally.values())
            logger.debug("batch exec failed after %d attempts: %r",
                         FLUSH_MAX_RETRIES, last_exc)

    pending: List[asyncio.Task] = []

    for base in range(0, max_n, BATCH_SIZE):
        hi = min(base + BATCH_SIZE, max_n)

        batch: GlideAnyBatch = batch_cls(is_atomic=False)
        tally = {"strings": 0, "hashes": 0, "sets": 0}

        for i in range(base, hi):
            if i < counts["strings"]:
                batch.set(f"{KEY_PREFIX}:str:{{{i % TOTAL_SLOTS}}}:{i}", rand_str(STRING_VALUE_BYTES))
                tally["strings"] += 1
            if i < counts["hashes"]:
                payload = {
                    f"f{n}": rand_str(HASH_FIELD_BYTES) for n in range(HASH_FIELDS)
                }
                batch.hset(f"{KEY_PREFIX}:hash:{{{i % TOTAL_SLOTS}}}:{i}", payload)
                tally["hashes"] += 1
            if i < counts["sets"]:
                members = [rand_str(SET_MEMBER_BYTES) for _ in range(SET_MEMBERS)]
                batch.sadd(f"{KEY_PREFIX}:set:{{{i % TOTAL_SLOTS}}}:{i}", members)
                tally["sets"] += 1

        batch_ops = sum(tally.values())
        if batch_ops == 0:
            continue

        pending.append(asyncio.create_task(flush(batch, tally)))
        done += batch_ops

        # drain completed tasks periodically so the list doesn't grow unbounded
        if len(pending) >= INFLIGHT_BATCHES * 2:
            await asyncio.gather(*pending)
            pending = []

        now = time.time()
        if now - last_report >= BATCH_REPORT_SECS:
            rate = done / max(now - start, 0.001)
            pct = 100.0 * done / max(total_ops, 1)
            eta = (total_ops - done) / max(rate, 1)
            logger.info(
                "progress %.1f%% ops=%d/%d rate=%.0f/s eta=%.0fs "
                "str=%d hash=%d set=%d err=%d",
                pct, done, total_ops, rate, eta,
                written["strings"], written["hashes"], written["sets"],
                written["errors"],
            )
            last_report = now

    if pending:
        await asyncio.gather(*pending)

    elapsed = time.time() - start
    logger.info(
        "done in %.1fs (%.0f ops/s) | strings=%d hashes=%d sets=%d errors=%d",
        elapsed, done / max(elapsed, 0.001),
        written["strings"], written["hashes"], written["sets"], written["errors"],
    )
    return written


async def main() -> int:
    """Entry point. Returns process exit code."""
    ap = argparse.ArgumentParser(
        description="Seed a cluster with mixed data types. "
                    "SANDBOX/TEST USE ONLY -- do not run against production."
    )
    ap.add_argument("--host", required=True, help="Cluster configuration endpoint")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--gb", type=float, default=5.0, help="Target logical GiB")
    ap.add_argument("--tls", action="store_true", default=True)
    ap.add_argument("--no-tls", dest="tls", action="store_false")
    ap.add_argument("--password", help="AUTH password (optional; omit if no auth)")
    ap.add_argument("--username", help="ACL/RBAC username (optional, requires --password)")
    args = ap.parse_args()

    if args.username and not args.password:
        logger.error("--username requires --password")
        return 1

    credentials = (
        ServerCredentials(password=args.password, username=args.username)
        if args.password
        else None
    )

    counts = plan_counts(args.gb)
    logger.info(
        "target=%.1f GiB -> strings=%d (1KiB) hashes=%d (%dx%dB) sets=%d (%dx%dB)",
        args.gb, counts["strings"], counts["hashes"], HASH_FIELDS,
        HASH_FIELD_BYTES, counts["sets"], SET_MEMBERS, SET_MEMBER_BYTES,
    )
    logger.info("log file: %s", _logfile)

    client = None
    try:
        client, is_cluster = await connect_auto(
            args.host, args.port, args.tls, credentials
        )
        pong = await client.ping()
        logger.info(
            "connected to %s:%d ping=%s mode=%s",
            args.host, args.port, pong, "cluster" if is_cluster else "standalone",
        )

        await warm_up(client, is_cluster)

        res = await seed(client, is_cluster, counts)
        return 1 if res["errors"] else 0

    except Exception as exc:  # noqa: BLE001
        logger.error("seed failed host=%s: %r", args.host, exc)
        return 1
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
