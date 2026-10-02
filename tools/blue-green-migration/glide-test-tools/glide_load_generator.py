#!/usr/bin/env python3
"""
Valkey Glide Slot Writer/Reader

*** SANDBOX / TEST USE ONLY -- DO NOT RUN AGAINST PRODUCTION ***

This drives sustained write and read load continuously until stopped,
competing with real application traffic. It overwrites keys named
slot:{N} for all 16384 slots. There is no dry-run and no undo.

Writes 1KB random values to all 16384 slots and reads them back.
Auto-detects cluster-mode enabled vs disabled, and RESP3 vs RESP2.
Supports retries, AZ affinity reads, local file logging, per-shard stats.

Usage:
    python3 glide_load_generator.py --host <endpoint> --duration 30
    python3 glide_load_generator.py --host <endpoint> --iterations 5
    python3 glide_load_generator.py --host <endpoint> --az us-east-1a
    python3 glide_load_generator.py --host <endpoint> --password <pw>
    python3 glide_load_generator.py --host <endpoint>  # runs until Ctrl+C
"""

import argparse
import asyncio
import logging
import random
import string
import time
import sys
from dataclasses import dataclass
from typing import Optional, Tuple
from glide import (
    GlideClient,
    GlideClientConfiguration,
    GlideClusterClient,
    GlideClusterClientConfiguration,
    NodeAddress,
    ProtocolVersion,
    ReadFrom,
    ServerCredentials,
)

TOTAL_SLOTS = 16384
KEY_PREFIX = "glidetest"  # namespace every key under this to avoid colliding with app data
VALUE_SIZE = 1024
INFLIGHT_LIMIT = 2000
MAX_RETRIES = 10

# Logging setup - console + local file (new file per run)
import os
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_filename = os.path.join(LOG_DIR, f"glide_load_generator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("glide_load_generator")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(log_filename)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def generate_1kb_string() -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=VALUE_SIZE))


@dataclass
class ShardStats:
    label: str = ""
    write_ok: int = 0
    write_fail: int = 0
    write_retries: int = 0
    read_ok: int = 0
    read_fail: int = 0
    read_retries: int = 0


async def retry_async(coro_fn, max_retries: int = MAX_RETRIES):
    """Retry an async operation up to max_retries times."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await coro_fn()
            return result, attempt
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(0.05 * attempt)
    raise last_err


async def discover_topology(client, is_cluster: bool):
    """Discover shard topology via CLUSTER SLOTS, or synthesize one for standalone.

    On a standalone (cluster-mode disabled) target, CLUSTER SLOTS is not
    meaningful -- there is exactly one node holding every slot. Report that
    as a single synthetic shard so the rest of the script (which reports
    per-shard stats) works unmodified.
    """
    if not is_cluster:
        shard = ShardStats(label=f"0-{TOTAL_SLOTS - 1}")
        return [shard], [0] * TOTAL_SLOTS

    result = await client.custom_command(["CLUSTER", "SLOTS"])
    node_ranges: dict[str, list[tuple[int, int]]] = {}

    for entry in result:
        start = int(entry[0])
        end = int(entry[1])
        master_info = entry[2]
        node_id = master_info[2].decode() if isinstance(master_info[2], bytes) else str(master_info[2])
        node_ranges.setdefault(node_id, []).append((start, end))

    shards: list[ShardStats] = []
    slot_to_shard = [0] * TOTAL_SLOTS
    sorted_nodes = sorted(node_ranges.items(), key=lambda x: min(r[0] for r in x[1]))

    for idx, (_, ranges) in enumerate(sorted_nodes):
        ranges.sort()
        label = ",".join(f"{s}-{e}" for s, e in ranges)
        shards.append(ShardStats(label=label))
        for s, e in ranges:
            for sl in range(s, e + 1):
                slot_to_shard[sl] = idx

    return shards, slot_to_shard


async def run_slot_test(client, is_cluster: bool, duration_sec: int = None, iterations: int = None):
    """Run SET/GET across all slots with concurrency control and retries."""
    pong = await client.ping()
    logger.info(f"PING response: {pong}")

    end_time = time.time() + duration_sec if duration_sec else float('inf')
    sweep = 0
    sem = asyncio.Semaphore(INFLIGHT_LIMIT)

    while True:
        if iterations is not None and sweep >= iterations:
            break
        if time.time() >= end_time:
            break

        sweep += 1
        shards, slot_to_shard = await discover_topology(client, is_cluster)

        write_ok = 0
        write_fail = 0
        write_retries = 0
        read_ok = 0
        read_fail = 0
        read_retries = 0

        logger.info(f"▶ Iteration #{sweep} start ({TOTAL_SLOTS} slots)")

        async def do_set(slot: int):
            nonlocal write_ok, write_fail, write_retries
            key = f"{KEY_PREFIX}:slot:{{{slot}}}"
            value = generate_1kb_string()
            shard_idx = slot_to_shard[slot]
            async with sem:
                try:
                    _, attempts = await retry_async(lambda: client.set(key, value))
                    write_ok += 1
                    shards[shard_idx].write_ok += 1
                    if attempts > 1:
                        write_retries += attempts - 1
                        shards[shard_idx].write_retries += attempts - 1
                except Exception:
                    write_fail += 1
                    shards[shard_idx].write_fail += 1

        async def do_get(slot: int):
            nonlocal read_ok, read_fail, read_retries
            key = f"{KEY_PREFIX}:slot:{{{slot}}}"
            shard_idx = slot_to_shard[slot]
            async with sem:
                try:
                    _, attempts = await retry_async(lambda: client.get(key))
                    read_ok += 1
                    shards[shard_idx].read_ok += 1
                    if attempts > 1:
                        read_retries += attempts - 1
                        shards[shard_idx].read_retries += attempts - 1
                except Exception:
                    read_fail += 1
                    shards[shard_idx].read_fail += 1

        tasks = []
        for slot in range(TOTAL_SLOTS):
            tasks.append(asyncio.create_task(do_set(slot)))
            tasks.append(asyncio.create_task(do_get(slot)))
            if slot % 1024 == 0 and slot > 0:
                logger.debug(
                    f"  progress slot={slot} SET(ok={write_ok}, fail={write_fail}) "
                    f"GET(ok={read_ok}, fail={read_fail})"
                )

        await asyncio.gather(*tasks)

        logger.info(
            f"▶ Iteration #{sweep} done | "
            f"SET ok={write_ok} fail={write_fail} retries={write_retries} | "
            f"GET ok={read_ok} fail={read_fail} retries={read_retries}"
        )
        for s in shards:
            logger.info(
                f"  Slots [{s.label}] | "
                f"SET ok={s.write_ok} fail={s.write_fail} retries={s.write_retries} | "
                f"GET ok={s.read_ok} fail={s.read_fail} retries={s.read_retries}"
            )

        await asyncio.sleep(1)

    logger.info("▶ Slot test finished")


async def connect_with_fallback(
    config_kwargs: dict,
) -> Tuple[Optional[object], str, bool]:
    """Connect using RESP3/RESP2 and cluster/standalone, whichever the target needs.

    Two independent fallbacks, tried together for each protocol attempt:

    1. RESP3 -> RESP2: Redis versions before 6.0 (e.g. 5.0.6) have no HELLO
       command, so glide cannot negotiate RESP3 and raises a connection
       error mentioning RESP3NotSupported. In that case we retry with RESP2.
    2. Cluster -> standalone: GlideClusterClient refuses to connect at all
       to a cluster-mode disabled target, raising "No topology views
       found" (confirmed empirically against a real standalone
       valkey-server). In that case we retry with GlideClient.

    Args:
        config_kwargs: Keyword args shared by both client configuration
            classes (addresses, use_tls, read_from, inflight_requests_limit,
            client_az), without a 'protocol' key.

    Returns:
        Tuple of (client, protocol_name, is_cluster). client is None if
        every combination failed.
    """
    for proto, proto_name in (
        (ProtocolVersion.RESP3, "RESP3"),
        (ProtocolVersion.RESP2, "RESP2"),
    ):
        # try cluster client first
        try:
            cfg = GlideClusterClientConfiguration(**config_kwargs, protocol=proto)
            client = await GlideClusterClient.create(cfg)
            return client, proto_name, True
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "No topology views found" in msg:
                logger.info(
                    "target is cluster-mode disabled, falling back to "
                    "standalone client (protocol=%s)", proto_name,
                )
                # fall through to try standalone client below with same protocol
            elif proto_name == "RESP3" and (
                "RESP3NotSupported" in msg or "HELLO" in msg
            ):
                logger.warning(
                    "RESP3 unsupported (server predates Redis 6.0), "
                    "retrying with RESP2"
                )
                continue  # try next protocol, cluster client again
            else:
                logger.error("connect failed with %s (cluster): %r", proto_name, exc)
                return None, proto_name, True

        # standalone attempt, same protocol
        try:
            standalone_kwargs = {
                k: v for k, v in config_kwargs.items() if k != "client_az"
            }
            cfg = GlideClientConfiguration(**standalone_kwargs, protocol=proto)
            client = await GlideClient.create(cfg)
            return client, proto_name, False
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if proto_name == "RESP3" and (
                "RESP3NotSupported" in msg or "HELLO" in msg
            ):
                logger.warning(
                    "RESP3 unsupported (server predates Redis 6.0), "
                    "retrying with RESP2"
                )
                continue
            logger.error("connect failed with %s (standalone): %r", proto_name, exc)
            return None, proto_name, False

    return None, "none", False


async def main():
    global KEY_PREFIX
    parser = argparse.ArgumentParser(
        description="Valkey Glide slot load test. "
                    "SANDBOX/TEST USE ONLY -- do not run against production."
    )
    parser.add_argument("--host", required=True, help="Cluster endpoint")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--tls", action="store_true", default=True)
    parser.add_argument("--no-tls", dest="tls", action="store_false")
    parser.add_argument("--password", help="AUTH password (optional; omit if no auth)")
    parser.add_argument("--username", help="ACL/RBAC username (optional, requires --password)")
    parser.add_argument(
        "--key-prefix",
        default=KEY_PREFIX,
        help=f"Prefix for every key written (default: {KEY_PREFIX!r}). "
             "Change this if the default collides with existing application keys.",
    )
    parser.add_argument("--duration", type=int, help="Duration in seconds")
    parser.add_argument("--iterations", type=int, help="Number of iterations")
    parser.add_argument("--az", type=str, help="AZ for affinity reads (e.g. us-east-1a)")
    parser.add_argument("--read-from", choices=["primary", "prefer_replica", "az_affinity"],
                        default="prefer_replica", help="Read strategy")
    args = parser.parse_args()

    KEY_PREFIX = args.key_prefix

    if args.duration and args.iterations:
        logger.error("Error: Provide either --duration OR --iterations, not both.")
        sys.exit(1)

    if args.username and not args.password:
        logger.error("--username requires --password")
        sys.exit(1)

    credentials = (
        ServerCredentials(password=args.password, username=args.username)
        if args.password
        else None
    )

    # Determine read strategy
    if args.az:
        read_from = ReadFrom.AZ_AFFINITY_REPLICAS_AND_PRIMARY
        client_az = args.az
        logger.info(f"Read strategy: AZ_AFFINITY_REPLICAS_AND_PRIMARY (az={client_az})")
    elif args.read_from == "prefer_replica":
        read_from = ReadFrom.PREFER_REPLICA
        client_az = None
        logger.info("Read strategy: PREFER_REPLICA")
    else:
        read_from = ReadFrom.PRIMARY
        client_az = None
        logger.info("Read strategy: PRIMARY")

    config_kwargs = dict(
        addresses=[NodeAddress(host=args.host, port=args.port)],
        use_tls=args.tls,
        read_from=read_from,
        inflight_requests_limit=5000,
    )
    if client_az:
        config_kwargs["client_az"] = client_az
    if credentials:
        config_kwargs["credentials"] = credentials

    client, protocol_used, is_cluster = await connect_with_fallback(config_kwargs)
    if client is None:
        logger.error("could not connect to %s:%d", args.host, args.port)
        return
    logger.info(
        f"Connected to {args.host}:{args.port} "
        f"(tls={args.tls}, protocol={protocol_used}, "
        f"mode={'cluster' if is_cluster else 'standalone'})"
    )

    try:
        await run_slot_test(client, is_cluster, duration_sec=args.duration, iterations=args.iterations)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
