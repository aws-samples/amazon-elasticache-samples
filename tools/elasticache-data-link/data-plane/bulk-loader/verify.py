"""Sampled check that the cache still matches the source (review finding C1).

After the first load nothing compared the cache with the source again, so rows
lost from the cache (a target node restarted without a replica, a FLUSHALL,
keys deleted or expired by another program) went unnoticed until a reader got
a missing or stale row. This reads a small sample of each table and checks it
both ways:

  source -> cache   a few windows of rows, each starting at a random primary
                    key (`WHERE pk >= <random> ORDER BY pk LIMIT n`): finds
                    rows the cache is missing or holds with different values;
  cache -> source   keys read from a random point of each primary's keyspace
                    (SCAN from a random cursor): finds rows the cache still
                    holds although the source deleted them or the filter now
                    drops them.

What the cache should hold is worked out exactly as a load works it out, with
the loader's own code: the value encoding (cdc_values), the key (sink_pk) and
the link's transforms. A row the filter drops must be ABSENT from the cache;
only its presence is reported. Only the fields the transforms produce are
compared; fields a denormalize rule embeds, the row version and fields the
cache holds besides are not.

A difference is reported only if it is still there when checked again 5 and
30 seconds later (VERIFY_RECHECK_SECONDS): a row changed while it was being
read, or a change the sink has not applied yet, is not an error. A table
whose load is running or did not finish is skipped.

Read only: it writes nothing to the source or the cache. Results are log lines
(one JSON line per table, which the loader stack's metric filters turn into
ECDL/Verify metrics) and an ERROR line naming example keys when something is
wrong. It always exits 0: what it found is a result, not a failure of the task.

Run by the control plane every VERIFY_INTERVAL_MINUTES on the loader stack's
verify task definition, or by hand:
  aws ecs run-task ... --task-definition ecdl-<link>-loader-verify
"""
import json
import os
import random
import sys
import time

import bulk_loader as bl
import cdc_values
import sqlident
import transforms

SOURCE_ROWS = int(os.environ.get("VERIFY_SOURCE_ROWS", "150") or 150)
CACHE_KEYS = int(os.environ.get("VERIFY_CACHE_KEYS", "50") or 50)
WINDOWS = int(os.environ.get("VERIFY_WINDOWS", "3") or 3)
RECHECK = [float(s) for s in (os.environ.get("VERIFY_RECHECK_SECONDS") or "5,30").split(",") if s.strip()]
EXAMPLES = 5
# Keys hold a row's hash; tombstones (<key>:ecdl-del) and indexes are other
# types and are never sampled.
TOMBSTONE_SUFFIX = ":ecdl-del"


def _s(v):
    return bl._as_str(v) if v is not None else None


def expected_fields(spec, table_name, encode, row):
    """The fields the cache must hold for a source row, or None when the
    link's filter drops the row (it must then be absent)."""
    out, keep = transforms.apply(spec, table_name, encode(row))
    return out if keep else None


def judge(expected, cached, skip=()):
    """'ok', 'missing', 'different' or 'extra' for one key.

    expected: the fields the row should have, None when it must be absent.
    cached:   the cached hash ({} when the key does not exist).
    skip:     fields whose format is not checked (see unchecked_columns).
    """
    if expected is None:
        return "extra" if cached else "ok"
    if not cached:
        return "missing"
    for field, value in expected.items():
        if field in skip:
            continue
        if cached.get(field) != value:
            return "different"
    return "ok"


def differing_fields(expected, cached, skip=()):
    return sorted(f for f, v in (expected or {}).items()
                  if f not in skip and (cached or {}).get(f) != v)


def split_key(suffix, ncolumns):
    """The key columns' values from a key suffix (sink_pk's inverse).

    A single column is the whole suffix. A composite key is split on ':'
    not preceded by an escaping backslash, and each part unescaped. Values
    come back in sink_pk's order: the column names sorted.
    """
    if ncolumns == 1:
        return [suffix]
    parts, cur, i = [], [], 0
    while i < len(suffix):
        ch = suffix[i]
        if ch == "\\" and i + 1 < len(suffix):
            cur.append(suffix[i + 1])
            i += 2
            continue
        if ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts if len(parts) == ncolumns else None


def unchecked_columns(src_type, columns):
    """Columns whose CDC format the loader does not know for certain; their
    values are not compared, so a format difference is not reported as data."""
    return {u.split(" (", 1)[0] for u in cdc_values.unverified_columns(src_type, columns)}


def load_unfinished(vk, progress_key):
    """True while the table's load is running or did not finish."""
    if not progress_key:
        return False
    raw = {(_s(k)): _s(v) for k, v in (vk.hgetall(progress_key) or {}).items()}
    if not raw:
        return False
    started = max(int(raw.get("started_at_ms") or 0), int(raw.get("resumed_at_ms") or 0))
    done = int(raw.get("completed_at_ms") or 0)
    return done < started


def cached_hash(vk, key):
    raw = vk.hgetall(key) or {}
    return {_s(k): _s(v) for k, v in raw.items()}


def primaries(vk):
    """One plain client per primary (a cluster) or the client itself."""
    get = getattr(vk, "get_primaries", None)
    if get is None:
        return [vk]
    return [vk.get_valkey_connection(node) for node in get()]


def sample_cache_keys(vk, key_prefix, n, rng=random):
    """Up to n row keys of a table, read from a random point of each primary's
    keyspace (SCAN accepts any cursor; one taken at random starts the
    iteration somewhere in the middle)."""
    match = bl._glob_escape(key_prefix) + "*"
    nodes = primaries(vk)
    per_node = max(1, -(-n // max(1, len(nodes))))
    found = []
    for node in nodes:
        got = []
        cursor = rng.getrandbits(24)
        for _ in range(8):
            cursor, keys = node.scan(cursor=cursor, match=match, count=500, _type="hash")
            got.extend(k for k in (_s(k) for k in keys) if not k.endswith(TOMBSTONE_SUFFIX))
            if len(got) >= per_node or int(cursor) == 0:
                break
        found.extend(got[:per_node])
    return found[:n]


class TableCheck(object):
    """Checks one table. Every source read goes through the loader's adapter."""

    def __init__(self, adapter, vk, src_type, table, table_name, key_prefix, spec,
                 requested_pk="", sleep=time.sleep, rng=random):
        self.adapter, self.vk, self.src_type = adapter, vk, src_type
        self.table, self.table_name, self.key_prefix, self.spec = table, table_name, key_prefix, spec
        self.requested_pk, self.sleep, self.rng = requested_pk, sleep, rng

    def prepare(self):
        self.pk, self.key_columns = bl.resolve_keys(self.adapter, self.table, self.requested_pk)
        sqlident.validate(self.pk, "chunk column")
        fold = getattr(self.adapter, "fold_identifier", None)
        if fold is not None:
            self.pk = fold(self.pk)
        self.columns = bl.read_columns(self.adapter, self.table)
        self.encode = bl.make_encoder(self.src_type, self.columns)
        # The row version is the sink's, never a source value (VERSION_FIELD).
        self.skip = unchecked_columns(self.src_type, self.columns) | {bl.VERSION_FIELD}

    def key_of(self, row):
        return self.key_prefix + bl.sink_pk(self.encode(row), self.key_columns)

    # ── reading the source ──────────────────────────────────────────────
    def source_sample(self):
        lo, hi = self.adapter.bounds(self.table, self.pk)
        if lo is None:
            return []
        per = max(1, SOURCE_ROWS // max(1, WINDOWS))
        rows, seen = [], set()
        integral = bl._is_integral(lo) and bl._is_integral(hi)
        for _ in range(WINDOWS if integral else 1):
            start = self.rng.randint(int(lo), int(hi)) if integral else lo
            got = self.adapter.page(self.table, self.pk, start, hi, per)
            if len(got) < per and integral and start > lo:
                got += self.adapter.page(self.table, self.pk, lo, start - 1, per - len(got))
            for row in got:
                key = self.key_of(row)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        return rows

    def source_row(self, key, chunk_value):
        """The source row behind a key now, or None when it is gone."""
        for row in self.adapter.value_rows(self.table, self.pk, chunk_value):
            if self.key_of(row) == key:
                return row
        return None

    def chunk_value_of_key(self, key):
        """The chunk column's value a key names, or None when it cannot be told."""
        values = split_key(key[len(self.key_prefix):], len(self.key_columns))
        if values is None:
            return None
        by_name = dict(zip(sorted(self.key_columns), values))
        text = by_name.get(self.pk)
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    # ── judging ─────────────────────────────────────────────────────────
    def state(self, key, chunk_value, row=None, reread=False):
        if reread or row is None:
            row = self.source_row(key, chunk_value) if chunk_value is not None else None
        want = expected_fields(self.spec, self.table_name, self.encode, row) if row is not None else None
        have = cached_hash(self.vk, key)
        return judge(want, have, self.skip), want, have

    def run(self):
        result = {"checked": 0, "missing": 0, "different": 0, "extra": 0, "examples": []}
        suspects = {}
        for row in self.source_sample():
            key = self.key_of(row)
            verdict, _, _ = self.state(key, row[self.pk], row=row)
            result["checked"] += 1
            if verdict != "ok":
                suspects[key] = row[self.pk]
        for key in sample_cache_keys(self.vk, self.key_prefix, CACHE_KEYS, self.rng):
            chunk_value = self.chunk_value_of_key(key)
            if chunk_value is None:
                continue
            result["checked"] += 1
            verdict, _, _ = self.state(key, chunk_value, reread=True)
            if verdict != "ok":
                suspects[key] = chunk_value
        # Only what is still wrong after every re-check is reported.
        for delay in RECHECK:
            if not suspects:
                break
            self.sleep(delay)
            suspects = {k: v for k, v in suspects.items()
                        if self.state(k, v, reread=True)[0] != "ok"}
        for key, chunk_value in sorted(suspects.items()):
            verdict, want, have = self.state(key, chunk_value, reread=True)
            if verdict == "ok":
                continue
            result[verdict] += 1
            if len(result["examples"]) < EXAMPLES:
                example = "{} {}".format(key, verdict)
                if verdict == "different":
                    example += " ({})".format(", ".join(differing_fields(want, have, self.skip)[:4]))
                result["examples"].append(example)
        return result


# ── the source's own capture health ─────────────────────────────────────
# Review findings H6 and H11. Neither shows anywhere else: Debezium's heartbeat
# keeps the link looking healthy while, on PostgreSQL, WAL piles up behind the
# slot until PostgreSQL invalidates it, or, on SQL Server, the Agent capture job
# has stopped and the change tables get nothing new.
PG_SLOT_SQL = ("SELECT wal_status, active, safe_wal_size, "
               "pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes "
               "FROM pg_replication_slots WHERE slot_name = %s")
PG_SLOT_SQL_OLD = ("SELECT active, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) "
                   "AS lag_bytes FROM pg_replication_slots WHERE slot_name = %s")
# Seconds since the newest change the capture job recorded. With the keep-alive
# (SqlServerKeepAlive) writing every 5 minutes, a running job stays under that.
MSSQL_CAPTURE_SQL = ("SELECT DATEDIFF(SECOND, sys.fn_cdc_map_lsn_to_time(sys.fn_cdc_get_max_lsn()), "
                     "GETDATE()) AS age")
MSSQL_STALE_SECONDS = int(os.environ.get("VERIFY_CAPTURE_STALE_SECONDS", "900") or 900)


def _num(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def source_health(adapter, src_type, env):
    """A {"ecdl_verify": "source", ...} record and a problem line (or None)."""
    if src_type == "postgresql":
        slot = (env.get("PG_SLOT_NAME") or "").strip()
        if not slot:
            return None, None
        try:
            rows = adapter.query_rows(PG_SLOT_SQL, (slot,))
        except Exception:                                                # noqa: BLE001
            rows = adapter.query_rows(PG_SLOT_SQL_OLD, (slot,))         # PostgreSQL 12
        record = {"slot": slot}
        if not rows:
            record["slot_missing"] = 1
            return record, ("replication slot {} does not exist. Debezium cannot resume from it; "
                            "see docs/operations.md, Switchovers, blue/green and major upgrades. (A link "
                            "deployed before per-link slots reads ecdl_slot instead: then ignore "
                            "this.)".format(slot))
        row = rows[0]
        record.update({"slot_lag_bytes": _num(row.get("lag_bytes")), "wal_status": row.get("wal_status"),
                       "safe_wal_size": _num(row.get("safe_wal_size")),
                       "slot_active": 1 if row.get("active") else 0})
        if row.get("wal_status") == "lost":
            return record, ("replication slot {} was invalidated (wal_status lost): the changes it had not "
                            "read are gone. Reload the link with a position reset.".format(slot))
        if row.get("wal_status") == "unreserved":
            return record, ("replication slot {} is past max_slot_wal_keep_size (wal_status unreserved, "
                            "{} bytes behind): PostgreSQL invalidates it at the next checkpoint unless "
                            "Debezium catches up.".format(slot, record["slot_lag_bytes"]))
        return record, None
    if src_type == "sqlserver":
        rows = adapter.query_rows(MSSQL_CAPTURE_SQL, ())
        age = _num(rows[0].get("age")) if rows else None
        record = {"capture_age_seconds": age}
        if age is None:
            return record, ("the CDC capture job has recorded no change at all "
                            "(sys.fn_cdc_get_max_lsn() is NULL): is SQL Server Agent running?")
        if age > MSSQL_STALE_SECONDS:
            return record, ("the CDC capture job recorded its last change {} s ago, although the "
                            "keep-alive writes every 5 minutes: the SQL Server Agent capture job is "
                            "probably stopped (EXEC sys.sp_cdc_help_jobs; msdb.dbo.sysjobactivity). "
                            "Nothing new reaches the change tables until it runs.".format(age))
        return record, None
    return None, None


def table_plan(env):
    """[(bare table, source table, requested chunk column, key prefix)]."""
    tables = [t.strip() for t in (env.get("TABLES") or "").split(",") if t.strip()]
    qualified = [t.strip() for t in (env.get("SOURCE_TABLES") or "").split(",") if t.strip()]
    keys = [k.strip() for k in (env.get("PRIMARY_KEYS") or "").split(",")]
    lead = (env.get("LINK_KEY_PREFIX") or "").strip().rstrip(":")
    out = []
    for i, bare in enumerate(tables):
        source = qualified[i] if len(qualified) == len(tables) else bare
        pk = keys[i] if i < len(keys) else ""
        prefix = "{}:{}:".format(lead, bare) if lead else "{}:".format(bare)
        out.append((bare, source, pk, prefix))
    return out


def emit(record):
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


def main(env=None, adapters=None, target=None, sleep=time.sleep, rng=random):
    env = os.environ if env is None else env
    adapters = adapters or bl.ADAPTERS
    src_type = env.get("SOURCE_ENGINE", "")
    link = (env.get("PROGRESS_KEY_BASE") or "").split(":")[1:2]
    link = link[0] if link else ""
    spec = None
    if env.get("TRANSFORMS_JSON"):
        spec = transforms.load_spec(env["TRANSFORMS_JSON"])
    source_cfg = {
        "host": env.get("SOURCE_DB_HOST", ""), "port": int(env.get("SOURCE_DB_PORT") or 0),
        "user": env.get("SOURCE_DB_USER", ""), "password": env.get("SOURCE_DB_PASSWORD", ""),
        "database": env.get("SOURCE_DB_NAME", ""), **bl.source_tls_from_env(),
    }
    print("ECDL verify: sampling {} table(s) of link {} (up to {} source rows and {} cache keys "
          "each); read only".format(len(table_plan(env)), link or "?", SOURCE_ROWS, CACHE_KEYS), flush=True)
    totals = {"checked": 0, "missing": 0, "different": 0, "extra": 0, "skipped": 0, "unchecked": 0}
    adapter = vk = None
    owned = target is None
    try:
        adapter = adapters[src_type](source_cfg)
        adapter.connect()
        vk = target if target is not None else bl.target_client(dict({
            "host": env["VALKEY_HOST"], "port": int(env.get("VALKEY_PORT", 6379)),
            "ssl": env.get("VALKEY_SSL", "true").lower() == "true",
            "cluster": env.get("VALKEY_CLUSTER", "true").lower() == "true",
        }, **bl.target_credential_from_env()))
    except Exception as exc:                                             # noqa: BLE001
        print("ERROR verify: could not connect ({}: {}); nothing was checked".format(
            type(exc).__name__, exc), flush=True)
        emit({"ecdl_verify": "run", "link": link, "unchecked": len(table_plan(env)), **{
            k: 0 for k in ("checked", "missing", "different", "extra", "skipped")}})
        return 0
    try:
        try:
            health, problem = source_health(adapter, src_type, env)
        except Exception as exc:                                         # noqa: BLE001
            health, problem = None, None
            print("ERROR verify: the source's capture health could not be read ({}: {})".format(
                type(exc).__name__, exc), flush=True)
        if health is not None:
            emit(dict({"ecdl_verify": "source", "link": link}, **health))
            if problem:
                print("ERROR verify: " + problem, flush=True)
        for bare, source, pk, prefix in table_plan(env):
            record = {"ecdl_verify": "table", "link": link, "table": bare,
                      "checked": 0, "missing": 0, "different": 0, "extra": 0}
            base = (env.get("PROGRESS_KEY_BASE") or "").strip()
            try:
                if load_unfinished(vk, bl.progress_key_for(base, prefix) if base else ""):
                    record["skipped"] = "load running or not finished"
                    totals["skipped"] += 1
                    emit(record)
                    continue
                check = TableCheck(adapter, vk, src_type, source, bare, prefix, spec, pk, sleep=sleep, rng=rng)
                check.prepare()
                found = check.run()
            except Exception as exc:                                     # noqa: BLE001
                print("ERROR verify: table {} could not be checked ({}: {})".format(
                    bare, type(exc).__name__, exc), flush=True)
                record["unchecked"] = 1
                totals["unchecked"] += 1
                emit(record)
                continue
            examples = found.pop("examples")
            record.update(found)
            for k in ("checked", "missing", "different", "extra"):
                totals[k] += found[k]
            emit(record)
            bad = found["missing"] + found["different"] + found["extra"]
            if bad:
                print("ERROR verify: table {}: {} of {} sampled row(s) wrong in the cache "
                      "({} missing, {} different, {} should be gone), still wrong after re-checks "
                      "{}s later: {}. Reload the table to repair it (`ecdl apply --reload`); see "
                      "docs/operations.md, Checking the cache against the source.".format(
                          bare, bad, found["checked"], found["missing"], found["different"],
                          found["extra"], "/".join("%g" % d for d in RECHECK), "; ".join(examples)),
                      flush=True)
            else:
                print("ECDL verify: table {}: {} sampled row(s) match".format(
                    bare, found["checked"]), flush=True)
    finally:
        if owned:
            bl._close_quietly(vk)
        if adapter is not None and getattr(adapter, "conn", None) is not None:
            bl._close_quietly(adapter.conn)
    emit(dict({"ecdl_verify": "run", "link": link}, **totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
