#!/usr/bin/env python3
"""
ECDL Parallel Bulk Loader — fast initial load for any relational source.

Reads an existing source table in parallel PK-range chunks and writes rows
directly to the Valkey target (pipelined HSET, either topology). This is the
"initial load" path that runs BEFORE Debezium CDC takes over for steady state.

Design:
  - Source is pluggable via a small SourceAdapter interface (snapshot only).
    MySQL and PostgreSQL are concrete here; Oracle/SQL Server implement
    the same interface with a different driver and bounds/chunk SQL.
  - The table is split into N ranges by primary key (min..max), and each range
    is loaded by a separate process (multiprocessing = real parallelism).
  - Writes go straight to the target Valkey cluster, pipelined.

Two-phase load sequence:
  1. Capture the source change-log position (binlog/LSN/SCN) — done by the
     orchestrator, not here.
  2. Run THIS loader (fast, parallel) to populate the cache.
  3. Start Debezium from the captured position (schema_only) for steady-state
     CDC. Overlapping changes are reconciled by idempotent HSET upserts.

Usage:
  python3 bulk_loader.py --config loader.json
  python3 bulk_loader.py --source mysql --table orders --pk id \
      --workers 16 --batch 5000
"""
import argparse
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# Validates and quotes table / column names. Vendored from ecdl_link/sqlident.py
# because this image does not contain ecdl_link.
import sqlident
# Renders each value the way the CDC path writes it (dates, booleans, binary).
import cdc_values

# Rows pulled from the source per fetch step.
#
# This is not merely a memory setting — it decides how many NETWORK ROUND TRIPS the
# read costs, and it does so unevenly across engines, which makes it a fairness
# problem when comparing sources:
#
#   MySQL      pymysql SSDictCursor  — server streams the result set; fetchmany()
#                                      pulls from the socket, no extra round trip
#   SQL Server pymssql fetchmany     — same, streams off the TDS connection
#   PostgreSQL psycopg2 named cursor — issues a `FETCH FORWARD n` per batch, so
#                                      every batch IS a round trip
#
# At the old default of 2000, a 5M-row load cost PostgreSQL ~2,500 round trips
# while MySQL and SQL Server needed almost none. Against a measured ~1.1 ms RTT
# that adds seconds of latency to one engine only — an artefact of the
# driver, not a property of the database. A large batch reduces the difference
# to a negligible amount (~100 round trips), so cross-engine comparisons measure the engine.
FETCH_BATCH = int(os.environ.get("FETCH_BATCH", "50000"))

# ─────────────────────────────────────────────────────────────────────────────
# Source adapters — snapshot capability only (parallel range read).
# ─────────────────────────────────────────────────────────────────────────────

class SourceAdapter:
    """Interface: bounds() -> (min_pk, max_pk); fetch(lo, hi) -> list[dict].

    Table and key names cannot be bound as parameters, so they are put into the
    SQL text by sqlident.render(), which rejects anything that is not a plain
    identifier and quotes each dotted part for this engine. The range bounds are
    always bind parameters.
    """
    QUOTE_STYLE = 'bare'
    BOUNDS_SQL = 'SELECT MIN({pk}) AS lo, MAX({pk}) AS hi FROM {t}'
    RANGE_SQL = 'SELECT * FROM {t} WHERE {pk} >= %s AND {pk} <= %s'

    # The first rows by key, for the memory estimate. Bounded by LIMIT rather
    # than by a key range, because key ranges are sparse (1 .. 1e9 with 6M rows).
    SAMPLE_SQL = 'SELECT * FROM {t} WHERE {pk} >= %s ORDER BY {pk} LIMIT %s'

    def estimate_rows(self, table):
        """Catalogue row estimate, or None. Cheap: never counts the table."""
        return None

    def sample(self, table, pk, lo, n):
        """Up to n rows from the start of the key range, as dicts."""
        raise NotImplementedError

    def _sql(self, template, table, pk):
        return sqlident.render(self.QUOTE_STYLE, template, t=table, pk=pk)

    def connect(self): raise NotImplementedError
    def bounds(self, table, pk): raise NotImplementedError

    def primary_key(self, table):
        """The table's declared primary key columns, in key order.

        Asked of the SOURCE rather than assumed, because the control plane
        frequently cannot introspect the database at all -- listing tables
        needs GetSecretValue on the source credential, and when that is
        missing the operator types the table name manually and no primary key
        is ever discovered. The loader has a working connection by
        definition, so it is the right place to find out.
        """
        raise NotImplementedError
    def fetch(self, table, pk, lo, hi): raise NotImplementedError

    def column_types(self, table):
        """[{name, type, precision, scale, column_type}] from the source catalogue.

        cdc_values uses this to write each value the way Debezium sends it: the
        driver returns the same Python datetime for a MySQL DATETIME and a
        TIMESTAMP, which CDC writes in two different formats.
        """
        return []

    def fold_identifier(self, name):
        """A column name as it appears in fetched rows and in change events."""
        return name


class MySQLAdapter(SourceAdapter):
    QUOTE_STYLE = 'mysql'

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import pymysql
        self.conn = pymysql.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 3306),
            user=self.cfg["user"], password=self.cfg["password"],
            database=self.cfg["database"], cursorclass=pymysql.cursors.DictCursor,
            # TIMESTAMP columns come back in the session time zone. CDC reads
            # them from the binlog, which stores UTC, and writes UTC, so read
            # them in UTC here too. DATETIME has no zone and is unaffected.
            init_command="SET time_zone = '+00:00'",
        )

    def primary_key(self, table):
        with self.conn.cursor() as c:
            c.execute(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
                (self.cfg["database"], table.split(".")[-1]))
            # MySQL 8 returns information_schema column names uppercase.
            return [(r.get("COLUMN_NAME") or r.get("column_name"))
                    for r in c.fetchall()]

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.BOUNDS_SQL, table, pk))
            r = c.fetchone()
            return r["lo"], r["hi"]

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            return c.fetchall()

    def column_types(self, table):
        import pymysql.cursors
        schema = table.split(".")[0] if "." in table else self.cfg["database"]
        with self.conn.cursor(pymysql.cursors.Cursor) as c:
            c.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, DATETIME_PRECISION, NUMERIC_SCALE, "
                "COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (schema, table.split(".")[-1]))
            return [{"name": r[0], "type": r[1], "precision": r[2], "scale": r[3],
                     "column_type": r[4]} for r in c.fetchall()]

    def estimate_rows(self, table):
        with self.conn.cursor() as c:
            c.execute("SELECT TABLE_ROWS AS n FROM information_schema.TABLES "
                      "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                      (table.split(".")[0] if "." in table else self.cfg["database"],
                       table.split(".")[-1]))
            r = c.fetchone()
            return int(r["n"]) if r and r.get("n") is not None else None

    def sample(self, table, pk, lo, n):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.SAMPLE_SQL, table, pk), (lo, n))
            return list(c.fetchall())

    def fetch_iter(self, table, pk, lo, hi, batch=50000):
        """Stream the range instead of buffering it.

        fetch() pulls the whole chunk into memory, and since the PK range is
        split one chunk per worker, all workers together hold the entire table
        at once — more workers does not reduce that, it just divides it. On a
        machine with less RAM than the table that means the OOM killer, which is
        how the OOM killer made a 4 vCPU / 8 GB instance unreachable mid-load.

        An unbuffered (server-side) cursor keeps memory at one batch per worker
        no matter how big the table is.
        """
        import pymysql.cursors
        cursor = self.conn.cursor(pymysql.cursors.SSDictCursor)
        try:
            cursor.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            while True:
                rows = cursor.fetchmany(batch)
                if not rows:
                    return
                for row in rows:
                    yield row
        finally:
            cursor.close()


class PostgreSQLAdapter(SourceAdapter):
    QUOTE_STYLE = 'postgresql'

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import psycopg2
        import psycopg2.extras
        self.conn = psycopg2.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 5432),
            user=self.cfg["user"], password=self.cfg["password"],
            dbname=self.cfg["database"],
        )
        self._dict = psycopg2.extras.RealDictCursor
        # Keep json/jsonb as the text PostgreSQL prints. Decoding it into a dict
        # and printing that again changes the spacing and key order, while CDC
        # carries the server's own text.
        psycopg2.extras.register_default_json(self.conn, loads=lambda s: s)
        psycopg2.extras.register_default_jsonb(self.conn, loads=lambda s: s)

    def column_types(self, table):
        with self.conn.cursor() as c:
            c.execute(
                "SELECT column_name, data_type, datetime_precision, numeric_scale "
                "FROM information_schema.columns WHERE (table_schema, table_name) = "
                "(SELECT n.nspname, c.relname FROM pg_class c "
                " JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE c.oid = to_regclass(%s))", (table,))
            return [{"name": r[0], "type": r[1], "precision": r[2], "scale": r[3]}
                    for r in c.fetchall()]

    def primary_key(self, table):
        with self.conn.cursor() as c:
            # to_regclass resolves a bare or schema-qualified name the same way
            # the loader's own queries do, so the answer matches what it reads.
            c.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                " AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)", (table,))
            return [r[0] for r in c.fetchall()]

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.BOUNDS_SQL, table, pk))
            lo, hi = c.fetchone()
            return lo, hi

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor(cursor_factory=self._dict) as c:
            c.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            return list(c.fetchall())

    def estimate_rows(self, table):
        with self.conn.cursor() as c:
            c.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
                      (table,))
            r = c.fetchone()
            # -1 (or 0) means never analysed: no estimate rather than a wrong one.
            return int(r[0]) if r and r[0] is not None and r[0] > 0 else None

    def sample(self, table, pk, lo, n):
        with self.conn.cursor(cursor_factory=self._dict) as c:
            c.execute(self._sql(self.SAMPLE_SQL, table, pk), (lo, n))
            return list(c.fetchall())

    def fetch_iter(self, table, pk, lo, hi, batch=50000):
        """Stream via a named (server-side) cursor - see the MySQL note above.

        Unlike the MySQL and SQL Server adapters, itersize here controls a real
        `FETCH FORWARD n` round trip per batch, so a small value adds round trips
        to this engine and no other. Keep it large; see the FETCH_BATCH note at
        the top of this module.
        """
        name = 'ecdl_bulk_{}_{}'.format(lo, hi)
        with self.conn.cursor(name=name, cursor_factory=self._dict) as c:
            c.itersize = batch
            c.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            for row in c:
                yield row


class SQLServerAdapter(SourceAdapter):
    """SQL Server / Azure SQL source.

    Uses pymssql rather than pyodbc (which an earlier note here suggested):
    pymssql ships manylinux wheels with FreeTDS bundled, so it installs with a
    plain pip install and no msodbcsql18 / unixODBC system packages. That
    matters because the loader can run on any host, including
    Amazon Linux, where the Microsoft ODBC packages are not fully supported.

    Identifiers are bracket-quoted, and schema-qualified names ("dbo.orders")
    are bracketed per part so [dbo].[orders] is produced instead of the
    invalid [dbo.orders].
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import pymssql
        self.conn = pymssql.connect(
            server=self.cfg["host"], port=int(self.cfg.get("port", 1433)),
            user=self.cfg["user"], password=self.cfg["password"],
            database=self.cfg["database"], as_dict=True,
        )

    QUOTE_STYLE = 'sqlserver'

    def primary_key(self, table):
        with self.conn.cursor(as_dict=True) as c:
            c.execute(
                "SELECT c.name FROM sys.indexes i "
                "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
                " AND ic.index_id = i.index_id "
                "JOIN sys.columns c ON c.object_id = ic.object_id "
                " AND c.column_id = ic.column_id "
                "WHERE i.is_primary_key = 1 AND i.object_id = OBJECT_ID(%s) "
                "ORDER BY ic.key_ordinal", (table,))
            return [r["name"] for r in c.fetchall()]

    # Fractional-second digits live in sys.columns.scale for these types.
    _FRACTIONAL = ("time", "datetime2", "datetimeoffset")

    def column_types(self, table):
        with self.conn.cursor(as_dict=True) as c:
            c.execute(
                "SELECT c.name AS name, t.name AS type, c.scale AS scale "
                "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
                "WHERE c.object_id = OBJECT_ID(%s)", (table,))
            return [{"name": r["name"], "type": r["type"], "scale": r["scale"],
                     "precision": r["scale"] if str(r["type"]).lower() in self._FRACTIONAL
                     else None} for r in c.fetchall()]

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.BOUNDS_SQL, table, pk))
            r = c.fetchone()
            return r["lo"], r["hi"]

    def fetch(self, table, pk, lo, hi):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            return c.fetchall()

    SAMPLE_SQL = 'SELECT TOP (%s) * FROM {t} WHERE {pk} >= %s ORDER BY {pk}'

    def estimate_rows(self, table):
        with self.conn.cursor() as c:
            c.execute("SELECT SUM(p.rows) AS n FROM sys.partitions p "
                      "WHERE p.object_id = OBJECT_ID(%s) AND p.index_id IN (0, 1)",
                      (table,))
            r = c.fetchone()
            return int(r["n"]) if r and r.get("n") is not None else None

    def sample(self, table, pk, lo, n):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.SAMPLE_SQL, table, pk), (n, lo))
            return list(c.fetchall())

    def fetch_iter(self, table, pk, lo, hi, batch=50000):
        """Stream the range in batches — see the MySQL note above for why.

        pymssql pulls rows off the TDS stream as they are requested, so looping
        on fetchmany() keeps resident memory at roughly one batch per worker.
        Calling fetchall() here would reintroduce exactly the whole-table-in-RAM
        problem that made a 4 vCPU / 8 GB instance unreachable.
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
            while True:
                rows = cursor.fetchmany(batch)
                if not rows:
                    return
                for row in rows:
                    yield row
        finally:
            cursor.close()


class OracleAdapter(SourceAdapter):
    """Oracle source (RDS Oracle or self-managed), via python-oracledb thin mode.

    Thin mode needs no Oracle Instant Client, so this installs with a plain pip
    install — the same reasoning that led to pymssql over pyodbc.

    Two Oracle-specific details:

    * **Identifiers come back UPPERCASE**, and are kept that way. `SELECT * FROM
      orders3w` yields ID, CATEGORY, ..., and Debezium's change events carry the
      same upper-case names, so the sink writes fields named ID, CATEGORY. This
      adapter used to lower-case them, which made a loaded row and a CDC-written
      row of the same table use different field names. fold_identifier()
      upper-cases a primary key given in lower case so row[pk] still finds it.
    * **arraysize is the round-trip setting.** Like psycopg2's itersize, each fetch of
      `arraysize` rows is a network round trip, so it is set from the caller's batch
      rather than left at the default 100 — which over 5M rows would be 50,000 round
      trips. See the FETCH_BATCH note at the top of this module.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import oracledb
        # service_name form: host:port/SERVICE
        svc = self.cfg.get("service") or self.cfg.get("database")
        dsn = f"{self.cfg['host']}:{self.cfg.get('port', 1521)}/{svc}"
        # NUMBER as Decimal (not a float that loses digits) and CLOB as text (not
        # a LOB handle), which is what Debezium sends for both.
        oracledb.defaults.fetch_decimals = True
        oracledb.defaults.fetch_lobs = False
        self.conn = oracledb.connect(user=self.cfg["user"],
                                     password=self.cfg["password"], dsn=dsn)

    def fold_identifier(self, name):
        # Unquoted Oracle identifiers are upper case (sqlident admits only
        # unquoted, plain names).
        return name.upper()

    def column_types(self, table):
        parts = table.split(".")
        with self.conn.cursor() as c:
            if len(parts) > 1:
                c.execute("SELECT column_name, data_type, data_precision, data_scale "
                          "FROM all_tab_columns WHERE owner = :1 AND table_name = :2",
                          [parts[0].upper(), parts[-1].upper()])
            else:
                c.execute("SELECT column_name, data_type, data_precision, data_scale "
                          "FROM all_tab_columns WHERE owner = "
                          "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AND table_name = :1",
                          [parts[-1].upper()])
            out = []
            for name, dtype, prec, scale in c.fetchall():
                dtype = str(dtype)
                # TIMESTAMP(6) keeps its fractional digits in data_scale.
                if dtype.upper().startswith("TIMESTAMP"):
                    prec = scale
                    dtype = "timestamp" + dtype[dtype.index(")") + 1:] if ")" in dtype else dtype
                out.append({"name": name, "type": dtype, "precision": prec, "scale": scale})
            return out

    # Validated but NOT quoted: an unquoted name is folded to upper case and
    # matches Oracle's catalog, so `orders3w` finds ORDERS3W. Quoting would force
    # an exact-case match and break every caller that passes a lower-case name.
    QUOTE_STYLE = 'bare'
    BOUNDS_SQL = 'SELECT MIN({pk}), MAX({pk}) FROM {t}'
    RANGE_SQL = 'SELECT * FROM {t} WHERE {pk} >= :1 AND {pk} <= :2'

    SAMPLE_SQL = 'SELECT * FROM {t} WHERE {pk} >= :1 ORDER BY {pk} FETCH FIRST :2 ROWS ONLY'

    def estimate_rows(self, table):
        with self.conn.cursor() as c:
            c.execute("SELECT num_rows FROM all_tables WHERE table_name = :1",
                      [table.split(".")[-1].upper()])
            r = c.fetchone()
            return int(r[0]) if r and r[0] is not None else None

    def sample(self, table, pk, lo, n):
        cur, rf = self._dict_cursor(n)
        try:
            cur.execute(self._sql(self.SAMPLE_SQL, table, pk), [lo, n])
            cur.rowfactory = rf
            return cur.fetchall()
        finally:
            cur.close()

    def _dict_cursor(self, batch):
        cur = self.conn.cursor()
        cur.arraysize = batch
        cur.prefetchrows = batch + 1

        def rowfactory(*args):
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, args))

        return cur, rowfactory

    def primary_key(self, table):
        # Oracle folds unquoted identifiers to upper case, so the catalogue is
        # queried in upper case and answers in upper case, matching the rows.
        with self.conn.cursor() as c:
            c.execute(
                "SELECT cc.column_name FROM all_constraints ac "
                "JOIN all_cons_columns cc ON cc.constraint_name = ac.constraint_name "
                " AND cc.owner = ac.owner "
                "WHERE ac.constraint_type = 'P' AND ac.table_name = :1 "
                "ORDER BY cc.position",
                [table.split(".")[-1].upper()])
            return [str(r[0]) for r in c.fetchall()]

    def bounds(self, table, pk):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.BOUNDS_SQL, table, pk))
            lo, hi = c.fetchone()
            return lo, hi

    def fetch(self, table, pk, lo, hi):
        cur, rf = self._dict_cursor(1000)
        try:
            cur.execute(self._sql(self.RANGE_SQL, table, pk), [lo, hi])
            cur.rowfactory = rf
            return cur.fetchall()
        finally:
            cur.close()

    def fetch_iter(self, table, pk, lo, hi, batch=50000):
        """Stream the range — see the MySQL note above for why buffering the whole range runs out of memory."""
        cur, rf = self._dict_cursor(batch)
        try:
            cur.execute(self._sql(self.RANGE_SQL, table, pk), [lo, hi])
            cur.rowfactory = rf
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                for row in rows:
                    yield row
        finally:
            cur.close()


ADAPTERS = {"mysql": MySQLAdapter, "postgresql": PostgreSQLAdapter,
            "sqlserver": SQLServerAdapter, "oracle": OracleAdapter}


# ─────────────────────────────────────────────────────────────────────────────
# Worker — runs in its own process; loads one PK range into Valkey.
# ─────────────────────────────────────────────────────────────────────────────

def target_credential_from_env():
    """Read the target cache credential, mirroring the Go sink's rules exactly.

    VALKEY_USERNAME / VALKEY_PASSWORD take precedence if either is set. Otherwise the whole
    Secrets Manager value arrives in VALKEY_SECRET: a JSON object with a password
    is an RBAC user, anything else is a plain auth token. The whole value is
    injected rather than a JSON key because the two secret shapes differ and ECS
    can only select a key from a secret that has one.

    Kept deliberately identical to tools/go-sink/connect.go loadCredential: the
    loader and the sink write to the same cache, so a credential that works for
    one and not the other would be a confusing partial failure.
    """
    username = (os.environ.get("VALKEY_USERNAME") or "").strip()
    password = os.environ.get("VALKEY_PASSWORD") or ""
    if username or password:
        return {"username": username, "password": password}

    raw = (os.environ.get("VALKEY_SECRET") or "").strip()
    if not raw:
        return {"username": "", "password": ""}  # nosec B105 - empty default, not a credential
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if not parsed.get("password"):
                raise SystemExit(
                    'VALKEY_SECRET is JSON but has no "password" field')
            return {"username": (parsed.get("username") or "").strip(),
                    "password": parsed["password"]}
    # A token can legitimately contain braces, so only a successful decode into
    # the expected shape counts as RBAC.
    return {"username": "", "password": raw}


def target_client(target_cfg):
    """Open a client for the target cache, matching its topology.

    Both shapes have to work. ElastiCache replication groups come cluster-mode
    enabled or disabled, and the two need different clients: a cluster client
    issues CLUSTER SLOTS and fails outright against a single node, while a plain
    client against a sharded cluster connects and then returns MOVED for most
    keys -- it would look like it was working and silently write almost nothing.

    Every target this loader had ever been pointed at was cluster-mode enabled,
    so the client was hardcoded and `mode: initial_load` against a non-sharded
    cache did not work at all.

    Topology is resolved from the target's ARN by the control plane and passed in;
    it is not a guess and not a customer setting.
    """
    clustered = target_cfg.get("cluster", True)
    kwargs = dict(
        host=target_cfg["host"], port=target_cfg.get("port", 6379),
        # Certificates verified: ElastiCache's Amazon-issued certificate covers
        # the configuration endpoint and every node address it returns.
        ssl=target_cfg.get("ssl", True), ssl_cert_reqs="required",
        ssl_check_hostname=bool(target_cfg.get("ssl", True)),
        decode_responses=False,
    )
    username = target_cfg.get("username") or None
    password = target_cfg.get("password") or None
    if password:
        kwargs["password"] = password
        if username:
            kwargs["username"] = username

    if clustered:
        from valkey.cluster import ValkeyCluster
        return ValkeyCluster(**kwargs)
    import valkey
    return valkey.Valkey(**kwargs)


def read_columns(adapter, table):
    """The table's column types, or [] for an adapter that cannot describe them."""
    if not hasattr(adapter, "column_types"):
        return []
    return adapter.column_types(table) or []


def make_encoder(src_type, columns):
    """row -> {field: text} in CDC's format; see cdc_values."""
    if not columns:
        return cdc_values.generic_encoder
    return cdc_values.row_encoder(src_type, columns)


def describe_conversions(src_type, columns):
    """One line naming the columns whose stored format differs from str(value)."""
    fmt = cdc_values.column_formatters(src_type, columns)
    if not fmt:
        return "none"
    types = {c["name"]: str(c.get("type")) for c in columns}
    return ", ".join(f"{n} ({types.get(n, '?')})" for n in sorted(fmt))


def _close_quietly(*conns):
    """Close connections from a failed attempt so a retry does not leak them."""
    for c in conns:
        closer = getattr(c, "close", None)
        if closer is None:
            continue
        try:
            closer()
        except Exception as exc:                                     # noqa: BLE001
            # Already broken, which is usually why the attempt failed. The retry
            # opens fresh connections either way.
            print(f"  (closing a connection from the failed attempt raised "
                  f"{type(exc).__name__}: {exc}; the retry uses a new one)", flush=True)


def _as_str(v):
    """Decode a value read back from the cache (decode_responses is off)."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


# Retrying a chunk. A bulk load is hours of reads and writes over two network
# hops, and it used to have no retry at all: one dropped connection, one cluster
# failover, one source timeout failed the chunk, which failed the table, which
# failed the deploy -- and the next attempt started again from row one.
#
# Now a chunk that fails is retried from the last position the TARGET CONFIRMED
# (the last pipeline that executed), with exponential backoff. Writes are
# idempotent HSETs keyed by primary key, so re-reading a few rows past that
# position is harmless. The error is raised only after LOAD_RETRIES attempts.
LOAD_RETRIES = int(os.environ.get("LOAD_RETRIES", "5"))
LOAD_BACKOFF_SEC = float(os.environ.get("LOAD_BACKOFF_SEC", "2"))
_sleep = time.sleep   # replaced in tests


def _is_permanent(exc):
    """Errors no retry can fix: bad config, bad SQL, a missing column."""
    if isinstance(exc, (SystemExit, KeyboardInterrupt, KeyError, TypeError,
                        ValueError, NameError, AttributeError, ImportError)):
        return True
    text = str(exc).lower()
    return any(s in text for s in (
        "unknown column", "doesn't exist", "does not exist", "access denied",
        "permission denied", "wrongtype", "noperm", "authentication",
        # A full target (noeviction) refuses every write until something frees
        # memory, which a retry 2-32s later will not do. Fail now, with the
        # error, instead of after five more refused attempts.
        "oom command not allowed"))


def load_range(args):
    """Load one chunk, retrying transient failures from the confirmed position."""
    lo = args[6]
    state = {"written": 0, "confirmed": None, "open": []}
    attempt = 0
    while True:
        start = lo if state["confirmed"] is None else _after(state["confirmed"], lo)
        try:
            _load_range_once(args, start, state)
            _close_quietly(*state.pop("open", []))
            return state["written"]
        except Exception as exc:                                     # noqa: BLE001
            # The failed attempt's connections are closed before the retry opens
            # new ones; they used to be left open, one source and one cache
            # connection per retry per chunk.
            _close_quietly(*state.pop("open", []))
            attempt += 1
            if _is_permanent(exc) or attempt > LOAD_RETRIES:
                print(f"ERROR chunk {args[10]} [{lo}..{args[7]}] failed after "
                      f"{attempt} attempt(s), {state['written']:,} rows written: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                raise
            delay = min(60.0, LOAD_BACKOFF_SEC * (2 ** (attempt - 1)))
            print(f"  chunk {args[10]} [{lo}..{args[7]}] failed ({type(exc).__name__}: "
                  f"{exc}); retry {attempt}/{LOAD_RETRIES} in {delay:.0f}s, "
                  + (f"from pk {start}" if state["confirmed"] is None else
                     f"after confirmed pk {state['confirmed']}"),
                  flush=True)
            _sleep(delay)


def _after(pk_value, default):
    """The first key after a confirmed one, for an integer range; otherwise
    restart at the chunk start (still correct, just re-reads more)."""
    try:
        return int(pk_value) + 1
    except (TypeError, ValueError):
        return default


def _load_range_once(args, lo_override, state):
    (src_type, src_cfg, target_cfg, table, pk, key_prefix, lo, hi, spec, table_name,
     widx, progress_key) = args[:12]
    lo = lo_override
    # Optional 13th element, so existing callers that build a 12-tuple keep the
    # default merge behaviour.
    #
    # replace=False (default): HSET merges into an existing key. Fields the
    #   current spec produces are written, but a field an EARLIER spec produced
    #   (since removed or renamed) stays, and a row the filter now drops keeps
    #   whatever key an earlier load wrote for it.
    # replace=True: every key in the range is rebuilt -- DEL then HSET for kept
    #   rows, DEL for rows the filter drops -- so the cache matches the current
    #   spec exactly. This is what the CDC sink already does on a snapshot read.
    replace = bool(args[12]) if len(args) > 12 else False
    import transforms

    adapter = ADAPTERS[src_type](src_cfg)
    adapter.connect()
    state["open"] = [getattr(adapter, "conn", None)]
    # Optional 14th element: the column types main() already read, so each
    # worker does not ask the catalogue again.
    columns = args[13] if len(args) > 13 else read_columns(adapter, table)
    encode = make_encoder(src_type, columns)

    vk = target_client(target_cfg)
    state["open"].append(vk)

    # Streamed, not buffered: memory stays flat regardless of how many rows this
    # chunk covers. Falls back to fetch() for any adapter that lacks it.
    if hasattr(adapter, 'fetch_iter'):
        # Pass FETCH_BATCH explicitly. Relying on each adapter's default meant all
        # three ran at 2000, which silently cost PostgreSQL ~2,500 round trips
        # for work the other two did without extra round trips — see the FETCH_BATCH note above.
        rows = adapter.fetch_iter(table, pk, lo, hi, batch=FETCH_BATCH)
    else:
        rows = adapter.fetch(table, pk, lo, hi)

    # Denorm rules for which THIS table is the child. When present, each row must
    # also embed its parent's fields (read from the cache) and register itself in
    # the parent's reverse index -- the same two phases the Go sink runs on the
    # CDC path, so an initial load and steady-state CDC shape data identically.
    # This requires the parent table to be loaded first; the orchestrator enforces
    # that ordering (see load_order() / main()).
    denorm_rules = transforms.denorm_for(spec, table_name)

    # state["written"] counts across retries; state["confirmed"] is the last row
    # the target has CONFIRMED (its pipeline executed), where a retry resumes.
    last_pk = state["confirmed"] if state["confirmed"] is not None else lo

    def confirm(pk_value, n):
        state["confirmed"] = pk_value
        state["written"] += n

    def report(done=False):
        # Publish how far this worker has progressed. A bulk load that rewrites existing
        # keys changes no key count and no value, so from the outside it looks like
        # nothing is happening. This is the loader's own account of its progress.
        if not progress_key:
            return
        try:
            m = {
                f"w{widx}_last_pk": str(state["confirmed"] if state["confirmed"]
                                         is not None else last_pk),
                f"w{widx}_rows": str(state["written"]),
                "updated_at_ms": str(int(time.time() * 1000)),
            }
            if done:
                m[f"w{widx}_done"] = "1"
            vk.hset(progress_key, mapping=m)
        except Exception as exc:                                     # noqa: BLE001
            # Progress reporting must never fail the load, but a failure is
            # logged: without it a resumed load restarts more than it needs to,
            # and the dashboard shows the load as stuck.
            if not state.get("report_warned"):
                state["report_warned"] = True
                print(f"  WARNING chunk {widx}: could not publish progress to "
                      f"{progress_key} ({type(exc).__name__}: {exc}); the load "
                      f"continues", flush=True)

    if not denorm_rules:
        # Fast path: pipelined HSET, no per-row reads.
        pending = 0
        unconfirmed = 0          # rows written into the current pipeline
        pipe = vk.pipeline(transaction=False)
        for row in rows:
            # Render each value as CDC writes it (cdc_values), then apply the SAME
            # transform spec the sink uses, so a loaded row and a CDC-written row
            # are identical.
            row_str = encode(row)
            out, keep = transforms.apply(spec, table_name, row_str)
            key = f"{key_prefix}{row[pk]}"
            if keep:
                if replace:
                    pipe.delete(key)
                pipe.hset(key, mapping=out)
                unconfirmed += 1
                pending += 1
            elif replace:
                # Filtered out. A plain load leaves any key an earlier load wrote;
                # a replacing load removes it, as the sink does when a row stops
                # matching.
                pipe.delete(key)
                pending += 1
            else:
                continue
            last_pk = row[pk]
            if pending >= 1000:
                pipe.execute()
                confirm(last_pk, unconfirmed)
                pending = unconfirmed = 0
                pipe = vk.pipeline(transaction=False)
                report()
        if pending:
            pipe.execute()
            confirm(last_pk, unconfirmed)
        report(done=True)
        return state["written"]

    # Denorm path. Collect a batch, resolve every parent in ONE pipeline of
    # HMGETs, then write each child (with the embedded parent fields) and its
    # reverse-index entry in a second pipeline -- two round trips per batch rather
    # than two per row.
    drops = []  # keys of filtered-out rows, removed on a replacing load

    def flush(batch):
        if drops:
            dp = vk.pipeline(transaction=False)
            for key in drops:
                dp.delete(key)
            dp.execute()
            del drops[:]
        if not batch:
            return
        # Phase 1: one HMGET per (row, rule) that has a parent key.
        lp = vk.pipeline(transaction=False)
        lookups = []  # index back into batch: (row_i, join_i)
        for ri, (_pk, _out, joins) in enumerate(batch):
            for ki, (rule, ppk) in enumerate(joins):
                if ppk:
                    lp.hmget(transforms.parent_key_prefix(rule) + ppk,
                             rule.get("fields", []))
                    lookups.append((ri, ki))
        results = lp.execute() if lookups else []
        parent_by = {}
        for res, key in zip(results, lookups):
            parent_by[key] = res

        # Phase 2: embed parent fields, write the child, add it to the parent's
        # reverse index.
        wp = vk.pipeline(transaction=False)
        for ri, (row_pk, out, joins) in enumerate(batch):
            for ki, (rule, ppk) in enumerate(joins):
                vals = parent_by.get((ri, ki))
                parent = {}
                if vals:
                    for f, v in zip(rule.get("fields", []), vals):
                        parent[f] = _as_str(v)
                for k, v in transforms.embed_parent_fields(rule, parent).items():
                    out[k] = v
            if replace:
                wp.delete(f"{key_prefix}{row_pk}")
            wp.hset(f"{key_prefix}{row_pk}", mapping=out)
            for rule, ppk in joins:
                if ppk:
                    wp.sadd(
                        transforms.idx_key(
                            table_name, rule.get("parent_table", ""), ppk),
                        str(row_pk))
        wp.execute()

    batch = []
    for row in rows:
        row_str = encode(row)
        out, keep = transforms.apply(spec, table_name, row_str)
        if not keep:
            if replace:
                drops.append(f"{key_prefix}{row[pk]}")
                if len(drops) >= 1000:
                    flush([])
            continue
        row_pk = row[pk]
        joins = [(rule, row_str.get(rule.get("join_field", ""), ""))
                 for rule in denorm_rules]
        batch.append((row_pk, out, joins))
        last_pk = row_pk
        if len(batch) >= 1000:
            flush(batch)
            confirm(last_pk, len(batch))
            batch = []
            report()
    flush(batch)
    if batch:
        confirm(last_pk, len(batch))
    report(done=True)
    return state["written"]


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Memory check, run before the first write.
#
# A load into a cache too small for it used to run until the target refused
# writes ("OOM command not allowed when used memory > 'maxmemory'"), before finishing,
# leaving a partial table in a cache that now also refused the CDC sink's writes
# for every other link sharing it. The estimate below is deliberately simple and
# stated as an estimate: catalogue row count x the size of a sampled row as this
# loader would write it, plus Valkey's per-key overhead, against what the
# target's primaries report free.
# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_ROWS = 200
# Per key: the keyspace dict entry, the key object and the hash's listpack
# header. Per field: listpack entry headers. Then allocator fragmentation.
PER_KEY_OVERHEAD = 80
PER_FIELD_OVERHEAD = 4
FRAGMENTATION = 1.15
# Refuse when the estimate needs more than this share of the free memory.
FIT_MARGIN = 0.9


def estimate_row_bytes(rows, key_prefix, pk, spec, table_name, encode=None):
    """Mean bytes one cached row takes, from rows shaped as the load writes them."""
    import transforms
    encode = encode or cdc_values.generic_encoder
    sizes = []
    for row in rows:
        row_str = encode(row)
        out, keep = transforms.apply(spec, table_name, row_str)
        if not keep:
            continue
        key = f"{key_prefix}{row[pk]}"
        payload = sum(len(str(f).encode()) + len(str(v).encode()) + PER_FIELD_OVERHEAD
                      for f, v in out.items())
        sizes.append(len(key.encode()) + payload + PER_KEY_OVERHEAD)
    if not sizes:
        return 0, 0.0
    kept = len(sizes) / float(len(rows)) if rows else 0.0
    return FRAGMENTATION * sum(sizes) / len(sizes), kept


def target_memory(vk):
    """(free_bytes, used_bytes, max_bytes, primaries) summed over primaries.

    None when the target does not report a memory limit (maxmemory 0), in which
    case nothing can be said and the check is skipped.
    """
    try:
        from valkey.cluster import ValkeyCluster
        clustered = isinstance(vk, ValkeyCluster)
    except ImportError:                                              # pragma: no cover
        clustered = False
    if clustered:
        res = vk.info("memory", target_nodes=ValkeyCluster.PRIMARIES)
        # One primary comes back as that node's INFO; several as {node: INFO}.
        infos = [res] if "used_memory" in res else list(res.values())
    else:
        infos = [vk.info("memory")]
    used = sum(int(i.get("used_memory", 0)) for i in infos)
    limit = sum(int(i.get("maxmemory", 0)) for i in infos)
    if not limit or any(not int(i.get("maxmemory", 0)) for i in infos):
        return None
    # The fullest primary is what refuses first: keys spread evenly by slot.
    tightest = min(int(i["maxmemory"]) - int(i.get("used_memory", 0)) for i in infos)
    return max(0, tightest) * len(infos), used, limit, len(infos)


def _gb(n):
    return "{:.2f} GB".format(n / 1e9)


def check_fits(adapter, vk, table, pk, lo, key_prefix, spec, table_name, encode=None):
    """Raise SystemExit when the load clearly will not fit; print the estimate.

    Rows whose keys already exist are not counted again (a reload overwrites
    them), measured on the same sample.
    """
    if os.environ.get("LOAD_SKIP_MEMORY_CHECK", "").lower() in ("1", "true", "yes"):
        print("  memory check skipped (LOAD_SKIP_MEMORY_CHECK)")
        return
    try:
        rows = adapter.estimate_rows(table)
        sample = adapter.sample(table, pk, lo, SAMPLE_ROWS)
        mem = target_memory(vk)
    except Exception as exc:                                         # noqa: BLE001
        print(f"  memory check could not run ({exc}); loading anyway")
        return
    if not rows or not sample or mem is None:
        print("  memory check skipped: no row estimate, no sample or no memory "
              "limit reported by the target")
        return
    per_row, kept = estimate_row_bytes(sample, key_prefix, pk, spec, table_name, encode)
    keys = [f"{key_prefix}{r[pk]}" for r in sample]
    try:
        present = sum(1 for k in keys if vk.exists(k))
    except Exception:                                                # noqa: BLE001
        present = 0
    new_share = 1.0 - present / float(len(keys))
    needed = rows * kept * new_share * per_row
    free, used, limit, primaries = mem
    print(f"  memory check: ~{rows:,} rows x {per_row:,.0f} B "
          f"({kept:.0%} kept by the filter, {1 - new_share:.0%} already cached) "
          f"= ~{_gb(needed)} needed; target has ~{_gb(free)} free "
          f"({_gb(used)} of {_gb(limit)} used, {primaries} primary(ies))")
    if needed > FIT_MARGIN * free:
        raise SystemExit(
            f"FATAL: {table} will not fit in the target cache: it needs about "
            f"{_gb(needed)} and the target has about {_gb(free)} free "
            f"({_gb(used)} of {_gb(limit)} used). Nothing was written. Use a larger "
            f"node type or more shards, or point this link at another cache. "
            f"(Set LOAD_SKIP_MEMORY_CHECK=true to load anyway.)")


def resume_plan(previous, table, lo, hi, nchunks):
    """{chunk index: "done" | last confirmed pk} from a previous run's progress.

    Empty -- start fresh -- unless the previous run was for the same table, key
    range and chunk count AND did not complete. Anything else (another table,
    the table grew, a different worker count, a finished run) is not resumable:
    chunk boundaries would not line up.
    """
    if not previous or previous.get("completed_at_ms"):
        return {}
    if (previous.get("table") != str(table)
            or previous.get("pk_lo") != str(lo) or previous.get("pk_hi") != str(hi)
            or previous.get("workers") != str(nchunks)):
        return {}
    plan = {}
    for i in range(nchunks):
        if previous.get(f"w{i}_done") == "1":
            plan[i] = "done"
        elif previous.get(f"w{i}_last_pk") not in (None, ""):
            try:
                int(previous[f"w{i}_last_pk"])
            except ValueError:
                continue
            plan[i] = previous[f"w{i}_last_pk"]
    return plan


def chunk_ranges(lo, hi, n):
    """Split [lo, hi] into n contiguous integer ranges."""
    lo, hi = int(lo), int(hi)
    span = hi - lo + 1
    size = max(1, math.ceil(span / n))
    ranges = []
    start = lo
    while start <= hi:
        end = min(start + size - 1, hi)
        ranges.append((start, end))
        start = end + 1
    return ranges


def resolve_primary_key(probe, table, requested):
    """Decide which column to chunk the load by.

    This used to be the string "id", defaulted three layers up in the control
    plane and never checked. When the table's key was called something else the
    loader failed during the deploy with only a raw MySQL error -- "Unknown column 'id' in
    'field list'" -- several minutes into a deployment, naming neither the table
    nor what the key actually was.

    An explicit request is honoured even when it is not the declared primary key,
    because chunking by any indexed numeric column is legitimate; it is only
    reported. What is refused is guessing.
    """
    if requested and hasattr(probe, "fold_identifier"):
        requested = probe.fold_identifier(requested)
    try:
        detected = [c for c in (probe.primary_key(table) or []) if c]
    except Exception as exc:                              # noqa: BLE001
        # No catalogue access is not fatal when we were told what to use.
        if requested:
            print(f"  note: could not read the primary key of {table} ({exc}); "
                  f"using --pk {requested} as given")
            return requested
        raise SystemExit(
            f"FATAL: no --pk given and the primary key of {table} could not be "
            f"read ({exc}). Name it explicitly, or set "
            f"advanced.loader.primary_keys in the link.")

    if requested:
        if detected and requested not in detected:
            print(f"  note: --pk {requested} is not the primary key of {table} "
                  f"(that is {', '.join(detected)}); using {requested} as asked. "
                  f"It must be indexed and numeric or the load will be slow.")
        return requested

    if not detected:
        raise SystemExit(
            f"FATAL: {table} has no primary key, so there is no range to split "
            f"the load by and no key to store each row under. Add one, or name a "
            f"unique indexed numeric column with --pk / "
            f"advanced.loader.primary_keys.")
    if len(detected) > 1:
        raise SystemExit(
            f"FATAL: {table} has a composite primary key "
            f"({', '.join(detected)}). This loader splits work by a single "
            f"numeric range, so pick one of those columns with --pk / "
            f"advanced.loader.primary_keys.")
    print(f"  primary key of {table} discovered from the source: {detected[0]}")
    return detected[0]


def main():
    ap = argparse.ArgumentParser(description="ECDL parallel bulk loader")
    ap.add_argument("--config", help="JSON config file")
    ap.add_argument("--source", choices=list(ADAPTERS))
    ap.add_argument("--table")
    ap.add_argument("--pk", default="",
                    help="column to chunk by; default: discover the table's "
                         "primary key from the source")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--key-prefix", default=None, help="default: {table}:")
    ap.add_argument("--transforms", default=os.environ.get("TRANSFORMS_FILE"),
                    help="path to shared transforms.json (optional)")
    ap.add_argument("--min-pk", type=int, default=None,
                    help="only load rows with pk >= this (incremental load from a watermark)")
    ap.add_argument("--max-pk", type=int, default=None,
                    help="only load rows with pk <= this")
    ap.add_argument("--progress-key",
                    default=os.environ.get("PROGRESS_KEY", "ecdl:metadata:bulkload"),
                    help="target hash the loader publishes live progress to so a "
                         "dashboard can show load progress; empty string disables")
    ap.add_argument("--replace", action="store_true",
                    default=os.environ.get("REPLACE_EXISTING", "").strip().lower()
                    in ("1", "true", "yes", "on"),
                    help="rebuild every key in the range instead of merging into "
                         "it: DEL+HSET for kept rows, DEL for rows the filter "
                         "drops. Use when re-loading after a transform change, so "
                         "removed fields and newly filtered rows do not remain. "
                         "Also set by REPLACE_EXISTING=true.")
    ap.add_argument("--fresh", dest="resume", action="store_false",
                    default=os.environ.get("LOAD_RESUME", "true").strip().lower()
                    not in ("0", "false", "no", "off"),
                    help="ignore an incomplete previous run of the same load and "
                         "start from the first row (default: resume it; also "
                         "LOAD_RESUME=false)")
    ap.add_argument("--expected-rows", type=int,
                    default=int(os.environ.get("EXPECTED_ROWS", 0) or 0),
                    help="row count for the range, so progress can be a percentage")
    args = ap.parse_args()

    if args.config:
        cfg = json.load(open(args.config))
    else:
        cfg = {
            "source_type": args.source,
            "source": {
                "host": os.environ["SOURCE_DB_HOST"],
                "port": int(os.environ.get("SOURCE_DB_PORT", 3306)),
                "user": os.environ["SOURCE_DB_USER"],
                "password": os.environ["SOURCE_DB_PASSWORD"],
                "database": os.environ["SOURCE_DB_NAME"],
            },
            "target": dict({
                "host": os.environ["VALKEY_HOST"],
                "port": int(os.environ.get("VALKEY_PORT", 6379)),
                "ssl": os.environ.get("VALKEY_SSL", "true").lower() == "true",
                # Resolved from the target ARN by the control plane. Defaults to
                # true because every existing caller points at a cluster-mode
                # target; set it false for a single-shard replication group.
                "cluster": os.environ.get(
                    "VALKEY_CLUSTER", "true").lower() == "true",
            }, **target_credential_from_env()),
            "table": args.table,
            "pk": args.pk,
            "workers": args.workers,
        }

    src_type = cfg["source_type"]
    table = cfg["table"]
    pk = cfg["pk"]
    workers = cfg.get("workers", 16)
    key_prefix = args.key_prefix or cfg.get("key_prefix") or f"{table}:"

    # Table name used to look up transform rules (short name, e.g. "orders").
    table_name = table.split(".")[-1]
    import transforms
    spec = transforms.load_spec(args.transforms) if args.transforms else None

    print(f"ECDL bulk loader | source={src_type} table={table} pk={pk} workers={workers} "
          f"transforms={'on' if spec else 'off'} "
          f"mode={'replace' if args.replace else 'merge'}")

    # Refuse a name that is not a plain identifier before touching the source,
    # with a message that names it, rather than failing inside the first query.
    try:
        sqlident.validate_qualified(table, "table")
    except sqlident.UnsafeIdentifier as exc:
        raise SystemExit(f"FATAL: {exc}")

    # 1) Discover PK bounds
    probe = ADAPTERS[src_type](cfg["source"])
    probe.connect()
    pk = resolve_primary_key(probe, table, pk)
    try:
        sqlident.validate(pk, "primary key")
    except sqlident.UnsafeIdentifier as exc:
        raise SystemExit(f"FATAL: {exc}")
    pk = probe.fold_identifier(pk) if hasattr(probe, "fold_identifier") else pk

    # Column types, read once, so every value is written in the format CDC
    # writes it. Without them dates and times would come out in the driver's
    # format, which differs from CDC's -- so a load that cannot read them stops.
    try:
        columns = read_columns(probe, table)
    except Exception as exc:                                         # noqa: BLE001
        raise SystemExit(f"FATAL: could not read the column types of {table} "
                         f"({type(exc).__name__}: {exc}). They decide how dates, "
                         f"times, booleans and binary values are written.")
    if not columns:
        raise SystemExit(f"FATAL: the source catalogue lists no columns for {table}; "
                         f"check the table name and the loader user's grants.")
    print(f"  values written in CDC format: {len(columns)} columns; converted: "
          f"{describe_conversions(src_type, columns)}")
    lo, hi = probe.bounds(table, pk)
    if lo is None:
        print("Table is empty — nothing to load.")
        return
    # Optional watermark bounds: load only rows within [min-pk, max-pk]. Useful
    # for incremental loads (e.g. everything inserted after the last max PK).
    if args.min_pk is not None:
        lo = max(int(lo), args.min_pk)
    if args.max_pk is not None:
        hi = min(int(hi), args.max_pk)
    if int(lo) > int(hi):
        print(f"No rows in PK range [{args.min_pk}..{args.max_pk}] — nothing to load.")
        return
    ranges = chunk_ranges(lo, hi, workers)
    print(f"PK range [{lo}..{hi}] split into {len(ranges)} chunks")

    # Before the first write, check that the load fits in the target memory.
    check_fits(probe, target_client(cfg["target"]), table, pk, lo, key_prefix,
               spec, table_name, encode=make_encoder(src_type, columns))

    # 2) Load ranges in parallel
    start = time.time()

    # RESUME. The progress hash used to be wiped at the start of every run, so a
    # load that failed at 90% started again from nothing. If the previous run
    # for this exact table, key range and chunking did not complete, chunks it
    # finished are skipped and the rest restart after their last confirmed key.
    previous = {}
    if args.progress_key and args.resume:
        try:
            raw = target_client(cfg["target"]).hgetall(args.progress_key) or {}
            previous = {_as_str(k): _as_str(v) for k, v in raw.items()}
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (could not read previous progress, loading from the beginning: {exc})")
    plan = resume_plan(previous, table, lo, hi, len(ranges))
    if plan:
        skipped = sum(1 for p in plan.values() if p == "done")
        print(f"  resuming an incomplete load of {table}: {skipped} of {len(ranges)} "
              f"chunk(s) already done; the rest continue after their last "
              f"confirmed key (--fresh to start over)")

    tasks = []
    for i, r in enumerate(ranges):
        resume_at = plan.get(i)
        if resume_at == "done":
            continue
        chunk_lo = r[0] if resume_at is None else max(r[0], int(resume_at) + 1)
        if chunk_lo > r[1]:
            continue
        tasks.append((src_type, cfg["source"], cfg["target"], table, pk, key_prefix,
                      chunk_lo, r[1], spec, table_name, i, args.progress_key,
                      args.replace, columns))
    # Seed the run header so a watcher can tell a fresh run from a stale one and
    # work out a percentage rather than just a rising count.
    if args.progress_key and plan:
        try:
            target_client(cfg["target"]).hset(args.progress_key, mapping={
                "resumed_at_ms": str(int(start * 1000)),
                "updated_at_ms": str(int(start * 1000))})
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (progress key unavailable: {exc})")
    elif args.progress_key:
        try:
            hdr = target_client(cfg["target"])
            hdr.delete(args.progress_key)
            hdr.hset(args.progress_key, mapping={
                "started_at_ms": str(int(start * 1000)),
                "updated_at_ms": str(int(start * 1000)),
                "workers": str(len(ranges)),
                "pk_lo": str(lo), "pk_hi": str(hi),
                "table": str(table),
                "expected_rows": str(args.expected_rows or 0),
            })
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (progress key unavailable: {exc})")
    total = 0
    done = len(ranges) - len(tasks)
    nchunks = len(ranges)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(load_range, t) for t in tasks]
        for f in as_completed(futs):
            total += f.result()
            done += 1
            el = time.time() - start
            rate = total / el if el else 0
            print(f"  [{done:>2}/{nchunks} chunks done] {total:>12,} rows | {el:6.1f}s | {rate:>10,.0f} rows/sec",
                  flush=True)

    elapsed = time.time() - start
    rate = total / elapsed if elapsed else 0
    # Marks the run complete, so the next run starts fresh instead of resuming.
    if args.progress_key:
        try:
            target_client(cfg["target"]).hset(args.progress_key, mapping={
                "completed_at_ms": str(int(time.time() * 1000))})
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (could not mark the load complete: {exc})")
    print(f"\n{'='*56}")
    print(f"Loaded {total:,} rows in {elapsed:.1f}s  ({rate:,.0f} rows/sec)")
    print(f"{'='*56}")


if __name__ == "__main__":
    main()
