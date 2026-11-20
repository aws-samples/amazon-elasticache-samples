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
import multiprocessing
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

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
FETCH_BATCH = int(os.environ.get("FETCH_BATCH") or "50000")
# Set (advanced.loader.fetch_batch, the FetchBatch stack parameter): used as
# given. Unset: 50,000, lowered for wide rows so that every worker's batch fits
# in the task's memory (size_fetch_batch).
FETCH_BATCH_SET = bool((os.environ.get("FETCH_BATCH") or "").strip())

# Reads are paged (SourceAdapter.paged_rows) unless LOAD_PAGED=false, which
# streams each chunk with one statement as before: only for a chunk column
# without an index, where every page would scan the table.
LOAD_PAGED = os.environ.get("LOAD_PAGED", "true").strip().lower() not in (
    "0", "false", "no", "off")
# How long one read waits for a lock on the source before it fails and is
# retried (MySQL lock_wait_timeout, PostgreSQL lock_timeout).
LOCK_WAIT_SEC = int(os.environ.get("LOAD_LOCK_WAIT_SEC", "10"))

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
    # ORDER BY is required, not tuning. A retry resumes after the last key the
    # target confirmed, and a resumed run after each chunk's last confirmed key,
    # which is only correct if every row below that key was already read.
    # Without it only MySQL (InnoDB scans in primary-key order) was safe:
    # PostgreSQL, SQL Server and Oracle may return a range in any order, so a
    # transient failure skipped unread lower keys and the load still succeeded.
    RANGE_SQL = 'SELECT * FROM {t} WHERE {pk} >= %s AND {pk} <= %s ORDER BY {pk}'

    # The first rows by key, for the memory estimate. Bounded by LIMIT rather
    # than by a key range, because key ranges are sparse (1 .. 1e9 with 6M rows).
    SAMPLE_SQL = 'SELECT * FROM {t} WHERE {pk} >= %s ORDER BY {pk} LIMIT %s'

    # Bounded reads (paged_rows). One statement per page of at most `batch`
    # rows, each its own transaction (the connections are autocommit), so no
    # statement runs for hours holding a MySQL metadata lock and read view or a
    # PostgreSQL snapshot (xmin) that stops VACUUM. Each page is an index range
    # read from the last key read.
    PAGE_SQL = 'SELECT * FROM {t} WHERE {pk} >= %s AND {pk} <= %s ORDER BY {pk} LIMIT %s'
    VALUE_SQL = 'SELECT * FROM {t} WHERE {pk} = %s'

    def page_params(self, lo, hi, n):
        return (lo, hi, n)

    OFFSET_SQL = 'SELECT {pk} AS k FROM {t} WHERE {pk} >= %s ORDER BY {pk} LIMIT 1 OFFSET %s'

    def key_at_offset(self, table, pk, lo, offset):
        """The chunk column's value `offset` rows after lo, or None past the end."""
        rows = self.query_rows(self._sql(self.OFFSET_SQL, table, pk), (lo, offset))
        return next(iter(rows[0].values())) if rows else None

    def query_rows(self, sql, params):
        """Run one read and return its rows as dicts (each adapter)."""
        raise NotImplementedError

    def page(self, table, pk, lo, hi, n):
        """Up to n rows with lo <= pk <= hi, in key order."""
        return self.query_rows(self._sql(self.PAGE_SQL, table, pk), self.page_params(lo, hi, n))

    def value_rows(self, table, pk, value):
        """Every row whose chunk column is `value`."""
        return self.query_rows(self._sql(self.VALUE_SQL, table, pk), (value,))

    def paged_rows(self, table, pk, lo, hi, batch, unique):
        """Rows lo..hi in key order, read one bounded page at a time.

        A unique chunk column continues after the last key of a full page. A
        column that repeats values cannot: the page may end inside a value. The
        rows of that last value are left for the next page, which starts at it;
        a value that fills a whole page by itself is read on its own.
        """
        batch = max(1, int(batch))
        while lo <= hi:
            rows = self.page(table, pk, lo, hi, batch)
            if len(rows) < batch:
                for row in rows:
                    yield row
                return
            last = rows[-1][pk]
            if unique:
                for row in rows:
                    yield row
                lo = last + 1
            elif rows[0][pk] == last:
                for row in self.value_rows(table, pk, last):
                    yield row
                lo = last + 1
            else:
                for row in rows:
                    if row[pk] != last:
                        yield row
                lo = last

    def estimate_rows(self, table):
        """Catalogue row estimate, or None. Cheap: never counts the table."""
        return None

    COUNT_SQL = 'SELECT COUNT(*) AS n FROM {t}'

    def count_rows(self, table):
        """COUNT(*) of the table, for when the catalogue has no estimate."""
        sql = sqlident.render(self.QUOTE_STYLE, self.COUNT_SQL, t=table)
        rows = self.query_rows(sql, ())
        return int(next(iter(rows[0].values()))) if rows else None

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


# ─────────────────────────────────────────────────────────────────────────────
# TLS to the source. Verified by default: the server certificate must chain to
# a CA in SOURCE_TLS_CA_FILE and name the host connected to. The default file is
# built into the image (Dockerfile): Amazon RDS's global CA bundle
# (rds-ca-bundle.pem, from truststore.pki.rds.amazonaws.com) followed by the
# image's system CAs, which cover RDS Proxy and sources with a public
# certificate. MySQL used to connect in plaintext and PostgreSQL with
# sslmode=prefer, which takes any certificate and falls back to plaintext, so
# the source password and every row could be read or changed in transit.
#
# SOURCE_TLS=false is the explicit opt-out, for a source without TLS (the
# control plane sets it from an external source's `tls: false`).
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_TLS_CA_DEFAULT = "/app/source-ca.pem"


def source_tls_from_env():
    """{"tls": bool, "tls_ca_file": path or None} from SOURCE_TLS(_CA_FILE)."""
    raw = (os.environ.get("SOURCE_TLS") or "true").strip().lower()
    if raw in ("false", "off", "0", "no", "disabled"):
        return {"tls": False, "tls_ca_file": None}
    if raw not in ("true", "on", "1", "yes", "verify", "verify-full"):
        raise SystemExit(f"FATAL: SOURCE_TLS={raw!r}: use true (verified TLS, the "
                         f"default) or false (no TLS)")
    return {"tls": True,
            "tls_ca_file": (os.environ.get("SOURCE_TLS_CA_FILE") or "").strip()
            or SOURCE_TLS_CA_DEFAULT}


def source_tls(cfg):
    """The CA file to verify the source with, or None for plaintext.

    Fails closed: a missing CA file stops the load instead of connecting
    without verification."""
    if cfg.get("tls", True) is False or str(cfg.get("tls")).lower() == "false":
        return None
    ca = cfg.get("tls_ca_file") or SOURCE_TLS_CA_DEFAULT
    if not os.path.isfile(ca):
        raise SystemExit(f"FATAL: the CA bundle for TLS to the source, {ca}, does not "
                         f"exist. Set SOURCE_TLS_CA_FILE to a PEM file of the CAs "
                         f"that sign the source's certificate (the loader image has "
                         f"{SOURCE_TLS_CA_DEFAULT}), or SOURCE_TLS=false for a "
                         f"source without TLS.")
    return ca


def describe_source_tls(cfg):
    ca = source_tls(cfg)
    if ca is None:
        return ("OFF (SOURCE_TLS=false): the connection to the source, including "
                "its password, is not encrypted")
    return f"verified (certificate and host name, CAs in {ca})"


def mysql_ssl_context(ca):
    """An SSLContext that verifies the server certificate and host name."""
    import ssl
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class MySQLAdapter(SourceAdapter):
    QUOTE_STYLE = 'mysql'

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        import pymysql
        ca = source_tls(self.cfg)
        tls = {"ssl": mysql_ssl_context(ca)} if ca else {}
        self.conn = pymysql.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 3306),
            user=self.cfg["user"], password=self.cfg["password"],
            database=self.cfg["database"], cursorclass=pymysql.cursors.DictCursor,
            # TIMESTAMP columns come back in the session time zone. CDC reads
            # them from the binlog, which stores UTC, and writes UTC, so read
            # them in UTC here too. DATETIME has no zone and is unaffected.
            # lock_wait_timeout: a read that waits behind a DDL's metadata lock
            # gives up and is retried, instead of queueing every later query on
            # the table behind it. Autocommit: each read is its own transaction,
            # so no read view or metadata lock outlives it (the probe connection
            # used to keep one open for the whole load).
            init_command="SET time_zone = '+00:00', lock_wait_timeout = {}".format(
                LOCK_WAIT_SEC),
            autocommit=True,
            **tls)

    def primary_key(self, table):
        with self.conn.cursor() as c:
            c.execute(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
                self._schema_and_name(table))
            # MySQL 8 returns information_schema column names uppercase.
            return [(r.get("COLUMN_NAME") or r.get("column_name"))
                    for r in c.fetchall()]

    def _schema_and_name(self, table):
        """(database, table) for the catalogue: "db.t" names its database,
        a bare name is in the connection's database."""
        parts = table.split(".")
        return (parts[-2] if len(parts) > 1 else self.cfg["database"]), parts[-1]

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
        with self.conn.cursor(pymysql.cursors.Cursor) as c:
            c.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, DATETIME_PRECISION, NUMERIC_SCALE, "
                "COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                self._schema_and_name(table))
            # IS_NULLABLE decides how a zero date is written (cdc_values).
            return [{"name": r[0], "type": r[1], "precision": r[2], "scale": r[3],
                     "column_type": r[4], "nullable": r[5]} for r in c.fetchall()]

    def estimate_rows(self, table):
        with self.conn.cursor() as c:
            c.execute("SELECT TABLE_ROWS AS n FROM information_schema.TABLES "
                      "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                      self._schema_and_name(table))
            r = c.fetchone()
            return int(r["n"]) if r and r.get("n") is not None else None

    def sample(self, table, pk, lo, n):
        with self.conn.cursor() as c:
            c.execute(self._sql(self.SAMPLE_SQL, table, pk), (lo, n))
            return list(c.fetchall())

    def query_rows(self, sql, params):
        with self.conn.cursor() as c:
            c.execute(sql, params)
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
        ca = source_tls(self.cfg)
        # verify-full: the certificate must chain to the CA file and name the
        # host. disable only when TLS is turned off on purpose.
        tls = ({"sslmode": "verify-full", "sslrootcert": ca} if ca
               else {"sslmode": "disable"})
        self.conn = psycopg2.connect(
            host=self.cfg["host"], port=self.cfg.get("port", 5432),
            user=self.cfg["user"], password=self.cfg["password"],
            dbname=self.cfg["database"],
            # A read that waits for a lock (behind a DDL) gives up and is retried.
            options="-c lock_timeout={}s".format(LOCK_WAIT_SEC), **tls)
        # Each read is its own transaction, so no snapshot (xmin) is held
        # between pages or while the probe connection sits open.
        self.conn.autocommit = True
        self._dict = psycopg2.extras.RealDictCursor
        # Keep json/jsonb as the text PostgreSQL prints. Decoding it into a dict
        # and printing that again changes the spacing and key order, while CDC
        # carries the server's own text.
        psycopg2.extras.register_default_json(self.conn, loads=lambda s: s)
        psycopg2.extras.register_default_jsonb(self.conn, loads=lambda s: s)
        # money, interval and the range types as the server's text too. CDC
        # parses the text of the first two (cdc_values.pg_money, pg_interval)
        # and sends ranges as their text; psycopg2 would make an interval a
        # timedelta of 30-day months and a range a Range object.
        import psycopg2.extensions
        for oid, name in ((790, "ECDL_MONEY"), (1186, "ECDL_INTERVAL"),
                          (3904, "ECDL_INT4RANGE"), (3926, "ECDL_INT8RANGE"),
                          (3906, "ECDL_NUMRANGE"), (3908, "ECDL_TSRANGE"),
                          (3910, "ECDL_TSTZRANGE"), (3912, "ECDL_DATERANGE")):
            psycopg2.extensions.register_type(
                psycopg2.extensions.new_type((oid,), name, lambda value, _cur: value),
                self.conn)

    @staticmethod
    def _regclass(table):
        """The name to give to_regclass(): quoted, as the loader's own queries
        quote it. to_regclass() parses its argument as SQL text, so an
        unquoted "Orders" was folded to orders and found nothing (or another
        table), while every query read "Orders"."""
        return sqlident.quote_qualified(table, 'postgresql')

    def column_types(self, table):
        # udt_name, the bit length and atttypmod pick the format of bit,
        # hstore and array columns (cdc_values); atttypmod is an array
        # column's declared precision, which information_schema leaves NULL.
        regclass = self._regclass(table)
        with self.conn.cursor() as c:
            c.execute(
                "SELECT ic.column_name, ic.data_type, ic.datetime_precision, "
                "ic.numeric_scale, ic.udt_name, ic.character_maximum_length, "
                "ic.is_nullable, a.atttypmod "
                "FROM information_schema.columns ic "
                "LEFT JOIN pg_attribute a ON a.attrelid = to_regclass(%s) "
                " AND a.attname = ic.column_name "
                "WHERE (ic.table_schema, ic.table_name) = "
                "(SELECT n.nspname, c.relname FROM pg_class c "
                " JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE c.oid = to_regclass(%s))", (regclass, regclass))
            return [{"name": r[0], "type": r[1], "precision": r[2], "scale": r[3],
                     "udt": r[4], "length": r[5], "nullable": r[6], "typmod": r[7]}
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
                "ORDER BY array_position(i.indkey, a.attnum)", (self._regclass(table),))
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
                      (self._regclass(table),))
            r = c.fetchone()
            # -1 (or 0) means never analysed: no estimate rather than a wrong one.
            return int(r[0]) if r and r[0] is not None and r[0] > 0 else None

    def sample(self, table, pk, lo, n):
        with self.conn.cursor(cursor_factory=self._dict) as c:
            c.execute(self._sql(self.SAMPLE_SQL, table, pk), (lo, n))
            return list(c.fetchall())

    def query_rows(self, sql, params):
        with self.conn.cursor(cursor_factory=self._dict) as c:
            c.execute(sql, params)
            return list(c.fetchall())

    def fetch_iter(self, table, pk, lo, hi, batch=50000):
        """Stream via a named (server-side) cursor - see the MySQL note above.

        Unlike the MySQL and SQL Server adapters, itersize here controls a real
        `FETCH FORWARD n` round trip per batch, so a small value adds round trips
        to this engine and no other. Keep it large; see the FETCH_BATCH note at
        the top of this module.
        """
        name = 'ecdl_bulk_{}_{}'.format(lo, hi)
        # A named cursor needs a transaction, which an autocommit connection
        # does not open; this one read (LOAD_PAGED=false) runs in one.
        self.conn.autocommit = False
        try:
            with self.conn.cursor(name=name, cursor_factory=self._dict) as c:
                c.itersize = batch
                c.execute(self._sql(self.RANGE_SQL, table, pk), (lo, hi))
                for row in c:
                    yield row
            self.conn.commit()
        finally:
            self.conn.autocommit = True


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
            database=self.cfg["database"], as_dict=True, autocommit=True,
        )

    QUOTE_STYLE = 'sqlserver'
    PAGE_SQL = 'SELECT TOP (%s) * FROM {t} WHERE {pk} >= %s AND {pk} <= %s ORDER BY {pk}'
    OFFSET_SQL = ('SELECT {pk} AS k FROM {t} WHERE {pk} >= %s ORDER BY {pk} '
                  'OFFSET %s ROWS FETCH NEXT 1 ROWS ONLY')

    def page_params(self, lo, hi, n):
        return (n, lo, hi)

    def query_rows(self, sql, params):
        with self.conn.cursor(as_dict=True) as c:
            c.execute(sql, params)
            return list(c.fetchall())

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
    RANGE_SQL = 'SELECT * FROM {t} WHERE {pk} >= :1 AND {pk} <= :2 ORDER BY {pk}'

    SAMPLE_SQL = 'SELECT * FROM {t} WHERE {pk} >= :1 ORDER BY {pk} FETCH FIRST :2 ROWS ONLY'
    PAGE_SQL = ('SELECT * FROM {t} WHERE {pk} >= :1 AND {pk} <= :2 ORDER BY {pk} '
                'FETCH FIRST :3 ROWS ONLY')
    VALUE_SQL = 'SELECT * FROM {t} WHERE {pk} = :1'
    OFFSET_SQL = ('SELECT {pk} FROM {t} WHERE {pk} >= :1 ORDER BY {pk} '
                  'OFFSET :2 ROWS FETCH NEXT 1 ROWS ONLY')

    def page_params(self, lo, hi, n):
        return [lo, hi, n]

    def query_rows(self, sql, params):
        cur, rf = self._dict_cursor(FETCH_BATCH)
        try:
            cur.execute(sql, list(params))
            cur.rowfactory = rf
            return cur.fetchall()
        finally:
            cur.close()

    # The catalogue's owner: the schema the name gives, else the session's
    # current schema -- the one an unqualified name in a query resolves to.
    # Matching the table name alone could answer for another schema's table.
    @staticmethod
    def _owner_and_name(table):
        """(OWNER or None, TABLE) in the catalogue's upper case."""
        parts = table.split(".")
        return (parts[-2].upper() if len(parts) > 1 else None), parts[-1].upper()

    def estimate_rows(self, table):
        owner, name = self._owner_and_name(table)
        with self.conn.cursor() as c:
            if owner:
                c.execute("SELECT num_rows FROM all_tables "
                          "WHERE owner = :1 AND table_name = :2", [owner, name])
            else:
                c.execute("SELECT num_rows FROM all_tables WHERE owner = "
                          "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
                          "AND table_name = :1", [name])
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
        owner, name = self._owner_and_name(table)
        with self.conn.cursor() as c:
            # Constant statements; the owner and table are bound, never spliced.
            if owner:
                c.execute(self._PK_SQL_OWNER, [owner, name])
            else:
                c.execute(self._PK_SQL_CURRENT_SCHEMA, [name])
            return [str(r[0]) for r in c.fetchall()]

    _PK_SQL_OWNER = (
        "SELECT cc.column_name FROM all_constraints ac "
        "JOIN all_cons_columns cc ON cc.constraint_name = ac.constraint_name "
        " AND cc.owner = ac.owner WHERE ac.constraint_type = 'P' "
        "AND ac.owner = :1 AND ac.table_name = :2 ORDER BY cc.position")
    _PK_SQL_CURRENT_SCHEMA = (
        "SELECT cc.column_name FROM all_constraints ac "
        "JOIN all_cons_columns cc ON cc.constraint_name = ac.constraint_name "
        " AND cc.owner = ac.owner WHERE ac.constraint_type = 'P' "
        "AND ac.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
        "AND ac.table_name = :1 ORDER BY cc.position")

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
# The version every loaded row carries, in the sink's version field. The deployer
# sets LOAD_VERSION to the broker's clock at the moment capture was paused, as a
# stream id ("<ms>-0"): every change captured before the pause is older than the
# rows the load reads, every change after the resume is newer. The sink's version
# guard then skips the older ones instead of writing them over loaded data, and
# applies the newer ones. Unset (a load run by hand): rows carry no version, and
# any later change applies over them, as before.
VERSION_FIELD = os.environ.get("VERSION_FIELD", "_ecdl_v")


def load_version_from_env():
    """LOAD_VERSION, validated: a stream id, or None when unset."""
    raw = (os.environ.get("LOAD_VERSION") or "").strip()
    if not raw:
        return None
    ms, dash, seq = raw.partition("-")
    if not (ms.isdigit() and dash and seq.isdigit()):
        raise SystemExit(f"FATAL: LOAD_VERSION {raw!r} is not a stream id like 1700000000000-0")
    return raw


def stamp(fields, version):
    """The row's fields with the load version added (a copy; transforms may not set it)."""
    out = {k: v for k, v in fields.items() if k != VERSION_FIELD}
    if version:
        out[VERSION_FIELD] = version
    return out


# A replacing load rebuilds a row with one script call, so nothing can read or
# write the key between the delete and the new fields. As two commands (DEL,
# then HSET) a sink write could land in between: it found no row, wrote an
# older change in full, and the load's HSET then merged into it, keeping
# fields the current spec no longer produces. HSET is called in slices of 1000
# arguments because Lua's unpack() has a stack limit.
REPLACE_LUA = """
redis.call('DEL', KEYS[1])
for i = 1, #ARGV, 1000 do
  redis.call('HSET', KEYS[1], unpack(ARGV, i, math.min(i + 999, #ARGV)))
end
return 1
"""


def load_replace_script(vk):
    """SCRIPT LOAD the replace script on the target (every primary)."""
    sha = vk.script_load(REPLACE_LUA)
    return _as_str(sha)


def write_row(pipe, key, fields, replace_sha=None):
    """Queue one row: HSET (merge), or with replace_sha an atomic replace."""
    if replace_sha is None:
        pipe.hset(key, mapping=fields)
        return
    flat = []
    for field, value in fields.items():
        flat.extend((field, value))
    # execute_command: valkey-py's cluster pipeline refuses its evalsha()
    # method, but routes an EVALSHA command by its key.
    pipe.execute_command("EVALSHA", replace_sha, 1, key, *flat)


def sink_pk(encoded, key_columns):
    """The row's identity in its cache key, exactly as the Go sink builds it.

    pkFromMsg in tools/go-sink/main.go reads the change event's key, which
    holds every primary-key column, and joins the values with ":" in the
    column NAMES' sorted order (not key order). Each value is the text the
    event carries, which is what cdc_values renders for the same column, so
    `encoded` is the row after encode(). One column is just its value.

    The loader used to key every row by the one column it split the load by.
    For a composite key that was a key the sink never writes, and the same
    leading value overwrote one key for every row that shared it.
    tests/sink_pk_cases.json holds cases both programs are tested against.
    """
    return ":".join(encoded[c] for c in sorted(key_columns))


# LOAD_RETRIES counts failures in a row WITHOUT progress: an attempt that
# confirmed rows before it failed starts the count again. It used to count every
# failure in the chunk's life, so six brief failures spread over hours failed a
# chunk that was loading fine between them. LOAD_RETRIES_TOTAL still caps all
# failures of one chunk, so a chunk that keeps failing does stop.
LOAD_RETRIES = int(os.environ.get("LOAD_RETRIES", "5"))
LOAD_RETRIES_TOTAL = int(os.environ.get("LOAD_RETRIES_TOTAL", "50"))
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


# ─────────────────────────────────────────────────────────────────────────────
# Write verification (D6).
#
# A pipeline's reply only says the target PRIMARY applied the writes. If that
# primary fails before a replica has them, the promoted replica does not, and
# the loader used to resume after its last confirmed key and exit 0 with the
# rows missing. Two checks close that:
#
#   * after a failed attempt, before resuming, the last few confirmed batches
#     are read back and the chunk resumes from the first one the target no
#     longer has (the whole chunk if the loss reaches past them);
#   * after every chunk is done, each chunk's last batches and an evenly spread
#     sample of its keys are read back; a chunk with a missing key is loaded
#     again, and a loss that remains fails the load.
#
# "Has the write" means the key's version field is at least this load's version
# (a row a CDC change made newer also passes). A load without a version can only
# check that the key exists, which an earlier load's copy also satisfies.
# ─────────────────────────────────────────────────────────────────────────────
VERIFY_BATCHES = int(os.environ.get("LOAD_VERIFY_BATCHES", "5"))
VERIFY_SAMPLE = int(os.environ.get("LOAD_VERIFY_SAMPLE", "200"))
VERIFY_REPAIRS = int(os.environ.get("LOAD_VERIFY_REPAIRS", "2"))


def _version_tuple(value):
    """A stream id as (ms, seq), or None when it is not one."""
    text = _as_str(value) if value is not None else ""
    ms, dash, seq = (text or "").partition("-")
    if not (ms.isdigit() and dash and seq.isdigit()):
        return None
    return int(ms), int(seq)


def missing_writes(vk, kept, dropped=(), members=(), version=None):
    """What the target does not have of a batch, as a list of descriptions.

    kept     keys the batch wrote: each must carry this version or a newer one
             (or, without a version, exist)
    dropped  keys a replacing load deleted: each must be absent
    members  (set key, member) reverse-index entries the batch added
    """
    kept, dropped, members = list(kept), list(dropped), list(members)
    if not (kept or dropped or members):
        return []
    want = _version_tuple(version) if version else None
    pipe = vk.pipeline(transaction=False)
    for key in kept:
        if want:
            pipe.hget(key, VERSION_FIELD)
        else:
            pipe.exists(key)
    for key in dropped:
        pipe.exists(key)
    for set_key, member in members:
        pipe.sismember(set_key, member)
    replies = pipe.execute()
    out = []
    for key, reply in zip(kept, replies[:len(kept)]):
        if want:
            have = _version_tuple(reply)
            if have is None or have < want:
                out.append(f"{key} (version {_as_str(reply) if reply is not None else 'none'})")
        elif not reply:
            out.append(f"{key} (absent)")
    rest = replies[len(kept):]
    for key, reply in zip(dropped, rest[:len(dropped)]):
        if reply:
            out.append(f"{key} (deleted by this load, present again)")
    for (set_key, member), reply in zip(members, rest[len(dropped):]):
        if not reply:
            out.append(f"{set_key} lacks {member}")
    return out


class _Sample(object):
    """An evenly spread, bounded sample of the keys a chunk wrote.

    Deterministic: every `stride`-th key is kept, and when the sample is full the
    stride doubles and every other kept key is dropped, so it always spans the
    whole chunk in at most `size` keys.
    """

    def __init__(self, size):
        self.size, self.stride, self.seen, self.keys = max(1, size), 1, 0, []

    def add(self, key):
        if self.seen % self.stride == 0:
            self.keys.append(key)
            if len(self.keys) > self.size:
                self.keys = self.keys[::2]
                self.stride *= 2
        self.seen += 1


def _remember_batch(state, before, kept, dropped, members, rows, same_before=0):
    """Record a confirmed batch for read-back; keep the last VERIFY_BATCHES.

    same_before is state["same"] as it was at `before`, so a rewind to it
    can restore it."""
    recent = state.setdefault("recent", [])
    recent.append({"before": before, "kept": list(kept), "dropped": list(dropped),
                   "members": list(members), "rows": rows, "same_before": same_before})
    if len(recent) > VERIFY_BATCHES:
        del recent[0]
        state["trimmed"] = True
    sample = state.setdefault("sample", _Sample(VERIFY_SAMPLE))
    for key in kept:
        sample.add(key)


def rewind_to_target(state, vk, version):
    """After a failed attempt: resume from the first batch the target lost.

    Returns "intact", "batch" (resume before a remembered batch) or "chunk"
    (the loss reaches past the remembered batches, so the chunk starts again).
    Rows of the batches being redone are taken off the written count.
    """
    recent = state.get("recent") or []
    for i, batch in enumerate(recent):
        if not missing_writes(vk, batch["kept"], batch["dropped"], batch["members"], version):
            continue
        if i == 0 and state.get("trimmed"):
            state["confirmed"] = None
            state["written"] = 0
            state["same"] = 0
            state["recent"] = []
            state["trimmed"] = False
            return "chunk"
        state["confirmed"] = batch["before"]
        state["same"] = batch.get("same_before", 0)
        state["written"] -= sum(b["rows"] for b in recent[i:])
        del recent[i:]
        return "batch"
    return "intact"


def load_range(args):
    """Load one chunk, retrying transient failures; returns the rows written."""
    return load_chunk(args)["written"]


def load_chunk(args):
    """Load one chunk, retrying transient failures from the last position the
    target still has. Returns what the post-load check needs: rows written,
    the last batches and a sample of keys."""
    lo = args[6]
    unique = _chunk_is_unique(args)
    state = {"written": 0, "confirmed": None, "open": []}
    attempt = 0      # failures in a row without progress (LOAD_RETRIES)
    failures = 0     # every failure of this chunk (LOAD_RETRIES_TOTAL)
    while True:
        start = lo if state["confirmed"] is None else _after(state["confirmed"], lo, unique)
        if not unique and state["confirmed"] is not None:
            # Rows at the confirmed value are read and written again, so they
            # are taken off the count rather than counted twice.
            state["written"] -= state.get("same", 0)
            state["same"] = 0
        written_before = state["written"]
        try:
            _load_range_once(args, start, state)
            _close_quietly(*state.pop("open", []))
            sample = state.get("sample")
            return {"widx": args[10], "lo": lo, "hi": args[7], "written": state["written"],
                    "tail": state.get("recent") or [],
                    "sample": sample.keys if sample else []}
        except Exception as exc:                                     # noqa: BLE001
            # The failed attempt's connections are closed before the retry opens
            # new ones; they used to be left open, one source and one cache
            # connection per retry per chunk.
            _close_quietly(*state.pop("open", []))
            failures += 1
            # Rows confirmed in this attempt are progress: the count of
            # failures in a row starts again.
            attempt = 1 if state["written"] > written_before else attempt + 1
            if _is_permanent(exc) or attempt > LOAD_RETRIES or failures > LOAD_RETRIES_TOTAL:
                why = ("" if _is_permanent(exc) else
                       f" (more than LOAD_RETRIES_TOTAL={LOAD_RETRIES_TOTAL} in all)"
                       if failures > LOAD_RETRIES_TOTAL else
                       f" ({attempt} in a row without progress, more than "
                       f"LOAD_RETRIES={LOAD_RETRIES})")
                print(f"ERROR chunk {args[10]} [{lo}..{args[7]}] failed after "
                      f"{failures} attempt(s){why}, {state['written']:,} rows written: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                if "oom command not allowed" in str(exc).lower():
                    print(f"ERROR the target cache is full. The rows this load wrote "
                          f"(keys {args[5]}*) are still there, and while it is full "
                          f"the sink's writes for every link on this cache are "
                          f"refused too. See 'A load that filled the target' in "
                          f"docs/operations.md to delete them.", flush=True)
                raise
            delay = min(60.0, LOAD_BACKOFF_SEC * (2 ** (attempt - 1)))
            print(f"  chunk {args[10]} [{lo}..{args[7]}] failed ({type(exc).__name__}: "
                  f"{exc}); retry {attempt}/{LOAD_RETRIES} in {delay:.0f}s "
                  f"({failures} failure(s) so far), "
                  + (f"from pk {start}" if state["confirmed"] is None else
                     f"from pk {start} (confirmed up to {state['confirmed']})"),
                  flush=True)
            _sleep(delay)
            _rewind_after_failure(args, state, lo)


def _rewind_after_failure(args, state, lo):
    """Before a retry: read back the last batches and resume from a lost one."""
    if not state.get("recent"):
        return
    vk = None
    try:
        vk = target_client(args[2])
        outcome = rewind_to_target(state, vk, load_version_from_env())
    except Exception as exc:                                         # noqa: BLE001
        # The target cannot be read yet. Redo the remembered batches rather
        # than trust them; the post-load check covers anything older.
        recent = state["recent"]
        state["confirmed"] = recent[0]["before"]
        state["same"] = recent[0].get("same_before", 0)
        state["written"] -= sum(b["rows"] for b in recent)
        state["recent"] = []
        print(f"  chunk {args[10]}: could not read back the last batches "
              f"({type(exc).__name__}: {exc}); writing them again", flush=True)
        return
    finally:
        _close_quietly(vk)
    if outcome == "batch":
        print(f"  chunk {args[10]}: the target lost confirmed writes (a failover?); "
              f"resuming {'from pk ' + str(lo) if state['confirmed'] is None else 'after pk ' + str(state['confirmed'])}",
              flush=True)
    elif outcome == "chunk":
        print(f"  chunk {args[10]}: the target lost writes older than the last "
              f"{VERIFY_BATCHES} batches; loading the chunk again from pk {lo}",
              flush=True)


def _after(pk_value, default, unique=True):
    """Where a read restarts after a confirmed chunk value, for an integer range;
    otherwise the chunk start (still correct, just re-reads more).

    A unique chunk column (the table's one-column primary key) restarts at the
    next value. Any other column can hold the confirmed value on more rows than
    were confirmed, so the read restarts AT that value and writes those rows
    again; the writes are idempotent. Restarting after it would skip them.
    """
    try:
        return int(pk_value) + (1 if unique else 0)
    except (TypeError, ValueError):
        return default


def _is_integral(value):
    """True for a whole number (int, or a Decimal / float with no fraction)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    try:
        return value == int(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _key_columns(args):
    """The task's primary-key columns (element 15); [chunk column] for a task
    built without them, which is the old one-column behaviour."""
    if len(args) > 14 and args[14]:
        return list(args[14])
    return [args[4]]


def _chunk_is_unique(args):
    """True when the chunk column alone is the primary key, so its values are
    unique and strictly increase through the range."""
    return _key_columns(args) == [args[4]]


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
    # Optional 15th element: the table's primary-key columns. Rows are keyed
    # by all of them, as the sink keys them; `pk` is only the column the range
    # is split and ordered by.
    key_columns = _key_columns(args)
    unique = _chunk_is_unique(args)
    encode = make_encoder(src_type, columns)
    version = load_version_from_env()

    vk = target_client(target_cfg)
    state["open"].append(vk)
    # Loaded on every attempt: a primary promoted after a failover may not
    # have it, and NOSCRIPT then fails the attempt, which retries.
    replace_sha = load_replace_script(vk) if replace else None

    # Streamed, not buffered: memory stays flat regardless of how many rows this
    # chunk covers. Falls back to fetch() for any adapter that lacks it.
    if LOAD_PAGED and hasattr(adapter, 'paged_rows'):
        rows = adapter.paged_rows(table, pk, lo, hi, FETCH_BATCH, unique)
    elif hasattr(adapter, 'fetch_iter'):
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
    # The link's advanced.key_prefix. Index keys and default parent keys carry
    # it, exactly as the sink writes them (idxKey / parentKey in denorm.go).
    link_prefix = transforms.normalize_key_prefix(os.environ.get("LINK_KEY_PREFIX", ""))

    # state["written"] counts across retries; state["confirmed"] is the last row
    # the target has CONFIRMED (its pipeline executed), where a retry resumes.
    last_pk = state["confirmed"] if state["confirmed"] is not None else lo

    # Rows counted in state["written"] whose chunk value is the latest one
    # written. When the chunk column repeats values, a retry re-reads the rows
    # at the confirmed value, and this many come off the count (state["same"]).
    run = {"value": None, "count": 0}

    def track(pk_value, counted):
        if run["value"] is None or pk_value != run["value"]:
            run["value"], run["count"] = pk_value, 0
        if counted:
            run["count"] += 1

    def confirm(pk_value, n):
        state["confirmed"] = pk_value
        state["written"] += n
        state["same"] = run["count"] if run["value"] == pk_value else 0

    # Rows must arrive in ascending key order (RANGE_SQL orders them); resuming
    # from a confirmed key relies on it. An adapter or a database that breaks
    # this fails the chunk rather than letting a retry skip rows. A chunk
    # column that is not the whole primary key repeats values, so equal
    # neighbours are allowed for it.
    seen = {"pk": None}

    def check_order(pk_value):
        if not _is_integral(pk_value):
            # The ranges are integer ranges [lo..hi] that meet at whole numbers,
            # so a value such as 2.5 falls between two chunks and is never read.
            raise ValueError(
                f"{pk} {pk_value!r} is not a whole number; the load is split into "
                f"integer ranges of {pk}, which would skip it. Split by an integer "
                f"column (advanced.loader.primary_keys)")
        prev = seen["pk"]
        if prev is not None and not (pk_value > prev if unique else pk_value >= prev):
            raise ValueError(
                f"rows came back out of key order ({pk} {pk_value!r} after "
                f"{prev!r}); a retry from the last confirmed key could skip rows, "
                f"so the chunk is stopped")
        seen["pk"] = pk_value

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
        kept_keys, dropped_keys = [], []   # this pipeline's keys, for read-back
        pipe = vk.pipeline(transaction=False)

        def confirm_batch():
            before, same_before = state["confirmed"], state.get("same", 0)
            confirm(last_pk, unconfirmed)
            _remember_batch(state, before, kept_keys, dropped_keys, (), unconfirmed,
                            same_before)
        for row in rows:
            # Render each value as CDC writes it (cdc_values), then apply the SAME
            # transform spec the sink uses, so a loaded row and a CDC-written row
            # are identical.
            check_order(row[pk])
            row_str = encode(row)
            out, keep = transforms.apply(spec, table_name, row_str)
            key = f"{key_prefix}{sink_pk(row_str, key_columns)}"
            if keep:
                write_row(pipe, key, stamp(out, version), replace_sha)
                kept_keys.append(key)
                unconfirmed += 1
                pending += 1
            elif replace:
                # Filtered out. A plain load leaves any key an earlier load wrote;
                # a replacing load removes it, as the sink does when a row stops
                # matching.
                pipe.delete(key)
                dropped_keys.append(key)
                pending += 1
            else:
                continue
            last_pk = row[pk]
            track(last_pk, keep)
            if pending >= 1000:
                pipe.execute()
                confirm_batch()
                pending = unconfirmed = 0
                kept_keys, dropped_keys = [], []
                pipe = vk.pipeline(transaction=False)
                report()
        if pending:
            pipe.execute()
            confirm_batch()
        report(done=True)
        return state["written"]

    # Denorm path. Collect a batch, resolve every parent in ONE pipeline of
    # HMGETs, then write each child (with the embedded parent fields) and its
    # reverse-index entry in a second pipeline -- two round trips per batch rather
    # than two per row.
    # Filtered-out rows, removed on a replacing load: (key, row pk, joins).
    drops = []

    flushed_drops = []  # dropped keys deleted since the last confirmed batch

    def flush(batch):
        """Write a batch; returns its reverse-index entries, for read-back."""
        if drops:
            remove_dropped(list(drops))
            flushed_drops.extend(key for key, _pk, _joins in drops)
            del drops[:]
        if not batch:
            return []
        members = []
        # Phase 1: one HMGET per (row, rule) that has a parent key.
        lp = vk.pipeline(transaction=False)
        lookups = []  # index back into batch: (row_i, join_i)
        for ri, (_pk, _out, joins) in enumerate(batch):
            for ki, (rule, ppk) in enumerate(joins):
                if ppk:
                    # The parent's version too: it is stored on the child (pv
                    # field) so the sink applies only NEWER parent changes.
                    # Each column under the name the parent's rename gives it
                    # in the cache, as the sink reads it (Jira story F198).
                    lp.hmget(transforms.parent_key_prefix(rule, link_prefix) + ppk,
                             [transforms.parent_field_name(spec, rule, f)
                              for f in rule.get("fields", [])] + [VERSION_FIELD])
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
                pv = ""
                if vals:
                    fields = rule.get("fields", [])
                    for f, v in zip(fields, vals):
                        if v is not None:
                            parent[f] = _as_str(v)
                    if len(vals) > len(fields) and vals[len(fields)] is not None:
                        pv = _as_str(vals[len(fields)])
                for k, v in transforms.embed_parent_fields(rule, parent).items():
                    out[k] = v
                # Which parent this child points at, and the parent version it
                # embeds. The sink's fan-out applies only to a child whose fk
                # matches, so without these a loaded child never received a
                # later parent change.
                out[transforms.fk_field(rule)] = ppk or ""
                out[transforms.pv_field(rule)] = pv
            write_row(wp, f"{key_prefix}{row_pk}", stamp(out, version), replace_sha)
            for rule, ppk in joins:
                if ppk:
                    set_key = transforms.idx_key(
                        table_name, rule.get("parent_table", ""), ppk, link_prefix)
                    wp.sadd(set_key, str(row_pk))
                    members.append((set_key, str(row_pk)))
        wp.execute()
        return members

    def remove_dropped(rows):
        """Delete filtered-out children and take them out of their parents'
        reverse indexes (Jira story F082). The index entry used to stay, so every later
        change of that parent ran a fan-out for a row that was gone.

        The parent is the one the source row points at now and the one the
        cached row pointed at (its _ecdl_fk field), which differ when the row
        moved since the last load. The SREMs go before the DEL in one
        pipeline: a retry after a partial failure still finds the cached row.
        """
        fk_fields = [transforms.fk_field(rule) for rule in denorm_rules]
        rp = vk.pipeline(transaction=False)
        for key, _pk, _joins in rows:
            rp.hmget(key, fk_fields)
        stored = rp.execute()
        dp = vk.pipeline(transaction=False)
        for (key, row_pk, joins), cached in zip(rows, stored):
            for (rule, ppk), old in zip(joins, cached or [None] * len(joins)):
                for parent in {ppk or "", _as_str(old)}:
                    if parent:
                        dp.srem(transforms.idx_key(table_name, rule.get("parent_table", ""),
                                                   parent, link_prefix), str(row_pk))
            dp.delete(key)
        dp.execute()

    def confirm_denorm(batch, members):
        before, same_before = state["confirmed"], state.get("same", 0)
        if batch:
            confirm(last_pk, len(batch))
        _remember_batch(state, before, [f"{key_prefix}{b[0]}" for b in batch],
                        flushed_drops, members, len(batch), same_before)
        del flushed_drops[:]

    batch = []
    for row in rows:
        check_order(row[pk])
        row_str = encode(row)
        out, keep = transforms.apply(spec, table_name, row_str)
        # The sink's identity for the row: its key suffix and its reverse-index
        # member.
        row_pk = sink_pk(row_str, key_columns)
        joins = [(rule, row_str.get(rule.get("join_field", ""), ""))
                 for rule in denorm_rules]
        if not keep:
            if replace:
                drops.append((f"{key_prefix}{row_pk}", row_pk, joins))
                if len(drops) >= 1000:
                    flush([])
            continue
        batch.append((row_pk, out, joins))
        last_pk = row[pk]
        track(last_pk, True)
        if len(batch) >= 1000:
            confirm_denorm(batch, flush(batch))
            batch = []
            report()
    members = flush(batch)
    if batch or flushed_drops:
        confirm_denorm(batch, members)
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


def _embedded_fields(vk, spec, table_name, row_str, member):
    """The fields a denorm child row carries besides its own, and its
    reverse-index entries, as the load writes them (see _load_range_once):
    the parent's embedded fields (read from the cache, where the parent is
    already loaded), the fk and pv fields, and one set member per parent.

    The estimate used to count only the row's own fields, so a child that
    embeds a wide parent needed far more memory than the check said."""
    import transforms
    rules = transforms.denorm_for(spec, table_name)
    extra, index_bytes = {}, 0
    link_prefix = transforms.normalize_key_prefix(os.environ.get("LINK_KEY_PREFIX", ""))
    for rule in rules:
        ppk = row_str.get(rule.get("join_field", ""), "")
        parent, pv = {}, ""
        if ppk and vk is not None:
            try:
                fields = list(rule.get("fields", []))
                vals = vk.hmget(transforms.parent_key_prefix(rule, link_prefix) + ppk,
                                fields + [VERSION_FIELD])
                parent = {f: _as_str(v) for f, v in zip(fields, vals) if v is not None}
                pv = _as_str(vals[-1]) if vals and vals[-1] is not None else ""
            except Exception:                                        # noqa: BLE001
                parent = {}
        extra.update(transforms.embed_parent_fields(rule, parent))
        extra[transforms.fk_field(rule)] = ppk or ""
        extra[transforms.pv_field(rule)] = pv
        if ppk:
            # A set member, and a share of the set's own key.
            index_bytes += len(member.encode()) + 16
    return extra, index_bytes


def estimate_row_bytes(rows, key_prefix, pk, spec, table_name, encode=None,
                       key_columns=None, vk=None, version=None):
    """Mean bytes one cached row takes, from rows shaped as the load writes them:
    its fields after the transform, the version field, and for a denorm child
    the embedded parent fields and reverse-index entries."""
    import transforms
    encode = encode or cdc_values.generic_encoder
    key_columns = key_columns or [pk]
    sizes = []
    for row in rows:
        row_str = encode(row)
        out, keep = transforms.apply(spec, table_name, row_str)
        if not keep:
            continue
        member = sink_pk(row_str, key_columns)
        key = f"{key_prefix}{member}"
        extra, index_bytes = _embedded_fields(vk, spec, table_name, row_str, member)
        fields = dict(out, **extra)
        if version:
            fields[VERSION_FIELD] = version
        payload = sum(len(str(f).encode()) + len(str(v).encode()) + PER_FIELD_OVERHEAD
                      for f, v in fields.items())
        sizes.append(len(key.encode()) + payload + PER_KEY_OVERHEAD + index_bytes)
    if not sizes:
        return 0, 0.0
    kept = len(sizes) / float(len(rows)) if rows else 0.0
    return FRAGMENTATION * sum(sizes) / len(sizes), kept


def spread_sample(adapter, table, pk, lo, hi, n, parts=4):
    """About n rows from `parts` evenly spaced points of the key range.

    The sample used to be the first n keys only, which says nothing about a
    table whose later rows are wider (newer rows with more data, a backfill)."""
    if hi is None or int(hi) <= int(lo) or parts < 2:
        return list(adapter.sample(table, pk, lo, n))
    lo, hi = int(lo), int(hi)
    each = max(1, n // parts)
    seen, out = set(), []
    for k in range(parts):
        start = lo + (hi - lo) * k // parts
        for row in adapter.sample(table, pk, start, each):
            ident = repr(sorted(row.items(), key=lambda kv: str(kv[0])))
            if ident not in seen:
                seen.add(ident)
                out.append(row)
    return out


# Memory per worker. Every worker holds a page of FETCH_BATCH rows as the
# driver returned them plus their encoded text, and 16 workers x 50,000 wide
# rows ran a 4 GB task out of memory, which surfaced as a vague
# "BrokenProcessPool". The batch is sized from the sampled rows so that all
# workers' pages use at most half the task's memory.
MEMORY_SHARE = 0.5
MIN_FETCH_BATCH = 500


def _python_bytes(row):
    """Rough in-memory size of one row dict and its values."""
    import sys
    return sys.getsizeof(row) + sum(sys.getsizeof(k) + sys.getsizeof(v)
                                    for k, v in row.items())


def task_memory_bytes():
    """The task's memory limit: TASK_MEMORY_MB, else the cgroup's, else None."""
    mb = (os.environ.get("TASK_MEMORY_MB") or "").strip()
    if mb.isdigit():
        return int(mb) * 1024 * 1024
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if text.isdigit() and int(text) < (1 << 60):
            return int(text)
    return None


def size_fetch_batch(sample, encode, workers, memory=None):
    """Lower FETCH_BATCH so every worker's page fits; returns the batch used."""
    global FETCH_BATCH
    memory = memory if memory is not None else task_memory_bytes()
    if FETCH_BATCH_SET or not sample or not memory:
        print(f"  fetch batch: {FETCH_BATCH:,} rows"
              + (" (FETCH_BATCH)" if FETCH_BATCH_SET else ""))
        return FETCH_BATCH
    encode = encode or cdc_values.generic_encoder
    per_row = sum(_python_bytes(r) + _python_bytes(encode(r)) for r in sample) / len(sample)
    fits = int(MEMORY_SHARE * memory / max(1, workers) / max(1.0, per_row))
    batch = max(MIN_FETCH_BATCH, min(FETCH_BATCH, fits))
    print(f"  fetch batch: {batch:,} rows (rows take ~{per_row / 1024:,.1f} KiB in "
          f"memory; {workers} workers in {memory / 1048576:,.0f} MiB)")
    if batch != FETCH_BATCH:
        FETCH_BATCH = batch
        # Workers started with spawn import this module again and read it here.
        os.environ["FETCH_BATCH"] = str(batch)
    return batch


def table_rows(adapter, table):
    """The catalogue's row estimate, else COUNT(*), else None.

    A table that was never analysed has no estimate, which used to skip the
    memory check (and now the quantile split) altogether. COUNT(*) is exact
    and costs one index scan; it runs only then."""
    try:
        rows = adapter.estimate_rows(table)
    except Exception as exc:                                         # noqa: BLE001
        print(f"  (no catalogue row estimate: {type(exc).__name__}: {exc})")
        rows = None
    if rows:
        return rows
    counter = getattr(adapter, "count_rows", None)
    if counter is None:
        return None
    try:
        began = time.time()
        rows = counter(table)
        print(f"  {table}: no catalogue row estimate; counted {int(rows or 0):,} rows "
              f"in {time.time() - began:.1f}s")
        return rows
    except Exception as exc:                                         # noqa: BLE001
        print(f"  (could not count the rows of {table}: {type(exc).__name__}: {exc})")
        return None


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


def check_fits(adapter, vk, table, pk, lo, key_prefix, spec, table_name, encode=None,
               key_columns=None, hi=None, rows=None, sample=None, version=None,
               fresh=True):
    """Raise SystemExit when the load clearly will not fit; print the estimate.

    Rows whose keys already exist are not counted again (a reload overwrites
    them), measured on the same sample. The row count is the catalogue's
    estimate, or COUNT(*) without one (table_rows); the sample is spread over
    the key range (spread_sample). Returns the share of sampled rows already
    cached, or None when the check did not run.
    """
    if os.environ.get("LOAD_SKIP_MEMORY_CHECK", "").lower() in ("1", "true", "yes"):
        print("  memory check skipped (LOAD_SKIP_MEMORY_CHECK)")
        return None
    try:
        rows = rows if rows is not None else table_rows(adapter, table)
        if sample is None:
            sample = spread_sample(adapter, table, pk, lo, hi, SAMPLE_ROWS)
        mem = target_memory(vk)
    except Exception as exc:                                         # noqa: BLE001
        print(f"  memory check could not run ({exc}); loading anyway")
        return None
    if not rows or not sample or mem is None:
        print("  memory check skipped: no row count, no sample or no memory "
              "limit reported by the target")
        return None
    per_row, kept = estimate_row_bytes(sample, key_prefix, pk, spec, table_name, encode,
                                       key_columns, vk=vk, version=version)
    new_share = 1.0 - cached_share(vk, sample, key_prefix, key_columns or [pk], encode)
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
    return 1 - new_share


def cached_share(vk, sample, key_prefix, key_columns, encode=None):
    """The share of the sampled rows whose cache key already exists (0 on error)."""
    if not sample:
        return 0.0
    to_text = encode or cdc_values.generic_encoder
    keys = [f"{key_prefix}{sink_pk(to_text(r), key_columns)}" for r in sample]
    try:
        present = sum(1 for k in keys if vk.exists(k))
    except Exception:                                                # noqa: BLE001
        present = 0
    return present / float(len(keys))


def check_chunks(vk, results, version):
    """Read back every chunk's last batches and sample.

    Returns {widx: (resume_after, problems)}: resume_after is the confirmed key
    before the first lost batch, or None to load the chunk from its start.
    """
    bad = {}
    for result in results:
        tail = result.get("tail") or []
        for batch in tail:
            lost = missing_writes(vk, batch["kept"], batch["dropped"], batch["members"], version)
            if lost:
                bad[result["widx"]] = (batch["before"], lost)
                break
        lost = missing_writes(vk, result.get("sample") or [], version=version)
        if lost:
            # Somewhere inside the chunk: there is no closer point to resume from.
            bad[result["widx"]] = (None, lost)
    return bad


def verify_and_repair(target_cfg, tasks, results, version):
    """The check after every load: every chunk's last batches and an evenly
    spread sample of its keys must be on the target. A chunk that lost writes
    is loaded again from the first lost batch (or its start) and checked again;
    a loss that remains after VERIFY_REPAIRS rounds stops the load (exit 1).
    """
    by_widx = {t[10]: t for t in tasks}
    checked = sum(len(r.get("sample") or []) + sum(len(b["kept"]) + len(b["dropped"])
                                                     + len(b["members"])
                                                     for b in r.get("tail") or [])
                  for r in results)
    vk = target_client(target_cfg)
    try:
        bad = check_chunks(vk, results, version)
        rounds = 0
        while bad and rounds < VERIFY_REPAIRS:
            rounds += 1
            for widx in sorted(bad):
                resume_after, lost = bad[widx]
                task = list(by_widx[widx])
                start = task[6] if resume_after is None else max(
                    task[6], _after(resume_after, task[6], _chunk_is_unique(task)))
                print(f"  check: chunk {widx} is missing {len(lost)} write(s), e.g. "
                      f"{'; '.join(lost[:3])}. Loading it again from pk {start} "
                      f"(repair {rounds}/{VERIFY_REPAIRS})", flush=True)
                task[6] = start
                redone = load_chunk(tuple(task))
                results = [r for r in results if r["widx"] != widx] + [redone]
            bad = check_chunks(vk, [r for r in results if r["widx"] in bad], version)
    finally:
        _close_quietly(vk)
    if bad:
        lines = "; ".join(f"chunk {w}: {', '.join(lost[:3])}" for w, (_r, lost) in sorted(bad.items()))
        raise SystemExit(f"FATAL: the target does not have rows this load wrote, after "
                         f"{VERIFY_REPAIRS} repair(s): {lines}. Check the target's "
                         f"events (failover, eviction) and load again.")
    how = "at this load's version or newer" if version else "present (no load version: existence only)"
    print(f"  check: {checked:,} written keys read back from the target, all {how}",
          flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# The sweep after a replacing load (Jira story F072). A reload rebuilt the rows the source
# still has, but a row deleted at the source while capture could not see it (a
# position reset, a link created again over an old cache) kept its key forever.
# After a complete replacing load, every hash under the table's key prefix
# whose version field is older than the load's is not from this load and not
# from a newer change, so it is deleted. Keys without a version field (written
# without one, by hand or by an older loader) are counted and left alone.
# ─────────────────────────────────────────────────────────────────────────────
SWEEP = os.environ.get("LOAD_SWEEP", "true").strip().lower() not in ("0", "false", "no", "off")


def _glob_escape(text):
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


def sweep_stale(vk, key_prefix, floor, spec=None, table_name=None):
    """Delete hashes under key_prefix whose version is older than `floor`.

    Returns (deleted, unversioned). A denorm child's entries in its parents'
    reverse indexes are removed with it."""
    import transforms
    want = _version_tuple(floor)
    if not want:
        return 0, 0
    rules = transforms.denorm_for(spec, table_name) if table_name else []
    link_prefix = transforms.normalize_key_prefix(os.environ.get("LINK_KEY_PREFIX", ""))
    fk_fields = [transforms.fk_field(r) for r in rules]
    deleted = unversioned = 0
    batch = []

    def flush(keys):
        nonlocal deleted, unversioned
        pipe = vk.pipeline(transaction=False)
        for key in keys:
            pipe.hmget(key, [VERSION_FIELD] + fk_fields)
        replies = pipe.execute()
        dp = vk.pipeline(transaction=False)
        n = 0
        for key, vals in zip(keys, replies):
            have = _version_tuple(vals[0]) if vals and vals[0] is not None else None
            if have is None:
                unversioned += 1
                continue
            if have >= want:
                continue
            dp.delete(key)
            member = _as_str(key)[len(key_prefix):]
            for rule, fk in zip(rules, vals[1:]):
                if fk:
                    dp.srem(transforms.idx_key(table_name, rule.get("parent_table", ""),
                                               _as_str(fk), link_prefix), member)
            n += 1
        if n:
            dp.execute()
            deleted += n

    for key in vk.scan_iter(match=_glob_escape(key_prefix) + "*", count=1000, _type="hash"):
        batch.append(key)
        if len(batch) >= 500:
            flush(batch)
            batch = []
    if batch:
        flush(batch)
    return deleted, unversioned


# Progress hash. One per link and table, named after the keyspace it loads:
#   <base>:<key prefix without its last ":">
#   ecdl:shop:bulkload:orders        link shop, table orders
#   ecdl:shop:bulkload:v2:orders     the same with advanced.key_prefix v2
# The base is PROGRESS_KEY_BASE, which the loader stack sets to
# ecdl:<link>:bulkload; a run by hand uses ecdl:bulkload. Every link and table
# used to share ecdl:metadata:bulkload, so a load could resume from another
# link's or table's progress and skip chunks it never loaded.
PROGRESS_KEY_BASE_DEFAULT = "ecdl:bulkload"


def progress_key_for(base, key_prefix):
    """The progress hash of the table whose rows are keyed `key_prefix`<pk>."""
    return f"{base or PROGRESS_KEY_BASE_DEFAULT}:{key_prefix.rstrip(':')}"


def spec_hash(spec):
    """A short, stable hash of the transform spec ("none" without one)."""
    import hashlib
    if not spec:
        return "none"
    text = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_identity(table, key_prefix, spec, replace, chunk, key_columns, scope="",
                 stack=""):
    """What makes a run resumable by another: all of it must match.

    A run resumed under any difference would skip chunks the earlier run
    wrote differently: other keys (key prefix, key columns), other values
    (transform spec, replace mode), another split (chunk column, the
    --min-pk/--max-pk scope), or another link of the same name (the loader
    stack's id, LOADER_STACK_ID: a link deleted and created again has a new
    stack, and its cache may no longer hold what the old one loaded).

    The key range is not part of it. Any row inserted at the source moved
    MAX(pk), so a failed load almost never resumed; the chunk boundaries are
    now stored with the run (stored_chunks) and reused.
    """
    return {
        "table": str(table),
        "key_prefix": str(key_prefix),
        "spec_hash": spec_hash(spec),
        "replace": "1" if replace else "0",
        "chunk_column": str(chunk),
        "key_columns": ",".join(key_columns),
        "scope": str(scope),
        "stack": str(stack or ""),
    }


def stored_chunks(previous, identity):
    """The chunk ranges of a previous incomplete run with this identity, or None."""
    if not previous or previous.get("completed_at_ms"):
        return None
    if any(previous.get(k) != v for k, v in identity.items()):
        return None
    try:
        chunks = [(int(a), int(b)) for a, b in json.loads(previous.get("chunks") or "")]
    except (TypeError, ValueError):
        return None
    return chunks or None


def extend_chunks(chunks, lo, hi):
    """The stored chunks, plus a chunk for keys outside them now in [lo, hi].

    Rows inserted since the earlier run lie above its last chunk (or below
    its first). They get chunks of their own, numbered after the stored ones,
    so the stored chunks keep their numbers and their progress."""
    out = list(chunks)
    first, last = min(a for a, _ in chunks), max(b for _, b in chunks)
    if int(hi) > last:
        out.append((last + 1, int(hi)))
    if int(lo) < first:
        out.append((int(lo), first - 1))
    return out


def resume_plan(previous, nchunks):
    """{chunk index: "done" | last confirmed pk} from a previous run's progress."""
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


def _identity_differences(previous, identity):
    """Names of the identity fields a previous, incomplete run differs in."""
    return [k for k, v in identity.items() if previous.get(k) != v]


# Splitting by quantiles. Equal-width ranges over a sparse key (6M rows with
# ids from 1 to 1e9, or clustered above 1e9) gave most chunks no rows and put
# the load on one worker. When the key range is much wider than the row count,
# the boundaries are read from the key index instead: every (rows / n)-th key.
SPARSE_FACTOR = 2


def split_ranges(probe, table, pk, lo, hi, n, rows):
    """n integer ranges over [lo, hi]: equal width, or by quantile when sparse."""
    lo, hi = int(lo), int(hi)
    equal = chunk_ranges(lo, hi, n)
    if (n < 2 or not rows or (hi - lo + 1) <= SPARSE_FACTOR * rows
            or not hasattr(probe, "key_at_offset")):
        return equal
    step = max(1, int(rows) // n)
    bounds, at = [], lo
    try:
        for _ in range(n - 1):
            key = probe.key_at_offset(table, pk, at, step)
            if key is None:
                break
            key = int(key)
            if key <= at:
                key = at + 1
            if key > hi:
                break
            bounds.append(key)
            at = key
    except Exception as exc:                                         # noqa: BLE001
        print(f"  (could not read key quantiles, splitting by width: "
              f"{type(exc).__name__}: {exc})")
        return equal
    starts = [lo] + bounds
    ranges = [(a, b - 1) for a, b in zip(starts, bounds)] + [(starts[-1], hi)]
    print(f"  {table}: keys {lo}..{hi} are sparse for ~{int(rows):,} rows; "
          f"split by quantiles of {pk}")
    return ranges


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


def resolve_keys(probe, table, requested):
    """(chunk column, key columns): what the load splits by, and what it keys by.

    They are two different things. The KEY is always the table's whole
    primary key, because the sink keys a row by every primary-key column (see
    sink_pk); a loaded row under any other key is one CDC never updates or
    deletes. The CHUNK column only decides how the read is split into integer
    ranges, so it must be an indexed integer column, and need not be unique.

    The chunk column is `requested` (--pk / advanced.loader.primary_keys) when
    given, else the primary key's first column, which its index leads with.

    The key used to be the chunk column. That made a composite or non-key
    --pk store rows under keys the sink never writes, and the FATAL for a
    composite key told the operator to name one of its columns, after which
    every row sharing that column's value overwrote the same key.
    """
    if requested and hasattr(probe, "fold_identifier"):
        requested = probe.fold_identifier(requested)
    try:
        detected = [c for c in (probe.primary_key(table) or []) if c]
    except Exception as exc:                              # noqa: BLE001
        # No catalogue access is not fatal when we were told what to use.
        if requested:
            print(f"  note: could not read the primary key of {table} ({exc}); "
                  f"keying rows by --pk {requested} as given, which is only right "
                  f"if {requested} alone is the primary key")
            return requested, [requested]
        raise SystemExit(
            f"FATAL: no --pk given and the primary key of {table} could not be "
            f"read ({exc}). Grant the loader user read access to the catalogue, "
            f"or, if the primary key is one integer column, name it with "
            f"advanced.loader.primary_keys.")

    if not detected:
        if requested:
            print(f"  note: {table} has no primary key; keying rows by --pk "
                  f"{requested}. CDC keys a table without a primary key by its "
                  f"`id` column, so this matches CDC only if {requested} is id.")
            return requested, [requested]
        raise SystemExit(
            f"FATAL: {table} has no primary key, so there is no key to store each "
            f"row under that CDC would also use. Add a primary key, or name a "
            f"unique indexed integer column with --pk / "
            f"advanced.loader.primary_keys.")

    chunk = requested or detected[0]
    if requested and requested not in detected:
        print(f"  note: splitting {table} by {requested}, which is not in its "
              f"primary key; rows are keyed by the primary key "
              f"({', '.join(detected)}) as CDC keys them. {requested} must be "
              f"an indexed integer column or the load will be slow.")
    elif len(detected) > 1:
        print(f"  composite primary key of {table}: {', '.join(detected)}; rows "
              f"are keyed by all of them as CDC keys them, and the load is split "
              f"by {chunk}")
    else:
        print(f"  primary key of {table}"
              + (" discovered from the source" if not requested else "")
              + f": {detected[0]}")
    return chunk, detected


def check_chunk_bounds(table, chunk, key_columns, lo, hi):
    """Raise SystemExit unless the chunk column's bounds are whole numbers.

    The read is split into integer ranges, so a text, date or fractional column
    cannot be split by. Named here, before anything is written, with what to do.
    """
    if _is_integral(lo) and _is_integral(hi):
        return
    raise SystemExit(
        f"FATAL: the load of {table} is split into integer ranges of {chunk}, "
        f"and {chunk} holds {lo!r}..{hi!r}, which are not whole numbers. Name an "
        f"indexed integer column to split by with --pk / "
        f"advanced.loader.primary_keys. Rows are still keyed by the primary "
        f"key ({', '.join(key_columns)}), so the choice does not change any key.")


def run_chunks(ex, fn, tasks, on_result):
    """Run fn over tasks in the pool `ex`; on_result(result) as each finishes.

    The first chunk that fails stops the load: chunks not started are
    cancelled and running workers are terminated, then the error is raised.
    It used to wait for every other chunk first, so a load whose fate was
    already decided kept writing for hours before it exited with the error.
    """
    futs = [ex.submit(fn, t) for t in tasks]
    try:
        for f in as_completed(futs):
            on_result(f.result())
    except BaseException:
        stop_chunks(ex, futs)
        raise
    ex.shutdown(wait=True)


def stop_chunks(ex, futs):
    """Cancel chunks not started and terminate the workers of a failed load."""
    running = sum(1 for f in futs if f.running())
    cancelled = sum(1 for f in futs if f.cancel())
    print(f"  stopping the load: {cancelled} chunk(s) not started are cancelled, "
          f"{running} running chunk(s) are stopped", flush=True)
    ex.shutdown(wait=False, cancel_futures=True)
    # Repeated: a pool may still be starting a worker process when the first
    # ones are terminated.
    deadline = time.time() + 10
    while True:
        children = multiprocessing.active_children()
        if not children or time.time() > deadline:
            break
        for child in children:
            child.terminate()
        for child in children:
            child.join(1)


# A load's log line per finished chunk was its only sign of life: a chunk of a
# large table runs for hours, so the log, the dashboard (which read another
# key until Jira story F060) and the CLI all showed nothing. The ticker prints the
# progress hash's totals every LOAD_LOG_EVERY_SEC, and warns when the rows
# written have not moved for LOAD_STALL_SEC.
LOG_EVERY_SEC = float(os.environ.get("LOAD_LOG_EVERY_SEC", "60"))
STALL_SEC = float(os.environ.get("LOAD_STALL_SEC", "600"))


def progress_totals(raw):
    """(rows written, chunks done) from a progress hash."""
    rows = done = 0
    for key, value in (raw or {}).items():
        key, value = _as_str(key), _as_str(value)
        if key.startswith("w") and key.endswith("_rows"):
            try:
                rows += int(value)
            except ValueError:
                pass
        elif key.startswith("w") and key.endswith("_done") and value == "1":
            done += 1
    return rows, done


class ProgressTicker(object):
    """A thread that logs the load's progress and warns when it stalls."""

    def __init__(self, target_cfg, progress_key, nchunks, started, every=None,
                 stall=None, clock=time.time):
        import threading
        self.target_cfg, self.key, self.nchunks = target_cfg, progress_key, nchunks
        self.started, self.clock = started, clock
        self.every = LOG_EVERY_SEC if every is None else every
        self.stall = STALL_SEC if stall is None else stall
        self._stop = threading.Event()
        self._thread = None
        self.last_rows, self.moved_at, self.warned = None, clock(), False

    def start(self):
        import threading
        if not self.key or self.every <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="ecdl-progress", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self):
        vk = None
        while not self._stop.wait(self.every):
            try:
                if vk is None:
                    vk = target_client(self.target_cfg)
                self.tick(vk.hgetall(self.key) or {})
            except Exception as exc:                                 # noqa: BLE001
                print(f"  progress: could not read {self.key} ({type(exc).__name__}: "
                      f"{exc})", flush=True)
                _close_quietly(vk)
                vk = None
        _close_quietly(vk)

    def tick(self, raw):
        """Print one progress line (and a stall warning); returns the line."""
        now = self.clock()
        rows, done = progress_totals(raw)
        if rows != self.last_rows:
            self.last_rows, self.moved_at, self.warned = rows, now, False
        elapsed = max(1e-9, now - self.started)
        line = (f"  progress: {rows:,} rows written, {done}/{self.nchunks} chunks done, "
                f"{rows / elapsed:,.0f} rows/s over {elapsed / 60:.1f} min")
        print(line, flush=True)
        if not self.warned and now - self.moved_at >= self.stall:
            self.warned = True
            print(f"  WARNING: the load has written no rows for "
                  f"{(now - self.moved_at) / 60:.0f} min. Chunks still running are "
                  f"waiting on the source or the target: look for retry lines above, "
                  f"and at the source's locks and the target's CPU and memory.",
                  flush=True)
        return line


def install_stop_handler():
    """SIGTERM stops every worker at once and exits 143.

    The loader runs as PID 1 in its container, and PID 1 ignores a signal it
    installs no handler for. So `stop_task` (a timed-out load, a deploy that
    failed or was interrupted) did nothing for 30 s, until ECS sent SIGKILL,
    and the workers kept writing while the deployer believed the load had
    stopped. The deployer now waits for STOPPED before it resumes capture,
    which this makes quick.
    """
    def stop(signum, _frame):
        print("ECDL loader: stop requested (signal {}); stopping every worker now"
              .format(signum), flush=True)
        children = multiprocessing.active_children()
        for child in children:
            child.terminate()
        for child in children:
            child.join(5)
        os._exit(143)
    signal.signal(signal.SIGTERM, stop)


def main():
    install_stop_handler()
    ap = argparse.ArgumentParser(description="ECDL parallel bulk loader")
    ap.add_argument("--config", help="JSON config file")
    ap.add_argument("--source", choices=list(ADAPTERS))
    ap.add_argument("--table",
                    help="the table as the source names it: schema.table "
                         "(PostgreSQL, SQL Server, Oracle) or database.table "
                         "(MySQL); a bare name reads the connection's default")
    ap.add_argument("--pk", default="",
                    help="integer column to split the load by (default: the "
                         "first column of the table's primary key). Rows are "
                         "always keyed by the whole primary key, as CDC keys "
                         "them")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--key-prefix", default=None,
                    help="default: <table>: with the bare table name, as the sink "
                         "names keys")
    ap.add_argument("--transforms", default=os.environ.get("TRANSFORMS_FILE"),
                    help="path to shared transforms.json (optional)")
    ap.add_argument("--min-pk", type=int, default=None,
                    help="only load rows with pk >= this (incremental load from a watermark)")
    ap.add_argument("--max-pk", type=int, default=None,
                    help="only load rows with pk <= this")
    ap.add_argument("--progress-key",
                    default=os.environ.get("PROGRESS_KEY"),
                    help="target hash the loader publishes live progress to and "
                         "resumes from (default: <PROGRESS_KEY_BASE>:<key prefix>, "
                         "one per link and table); empty string disables")
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
                **source_tls_from_env(),
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
    # The table as the source names it ("sales.orders", "Inventory.orders") is
    # what every query and catalogue lookup uses. The bare name is what the sink
    # names keys after (the change event's source.table) and what transform
    # rules are keyed by, so keys and transforms use it.
    table_name = table.split(".")[-1]
    key_prefix = args.key_prefix or cfg.get("key_prefix") or f"{table_name}:"
    if args.progress_key is None:
        args.progress_key = progress_key_for(
            (os.environ.get("PROGRESS_KEY_BASE") or "").strip(), key_prefix)
    import transforms
    spec = transforms.load_spec(args.transforms) if args.transforms else None

    print(f"ECDL bulk loader | source={src_type} table={table} pk={pk} workers={workers} "
          f"transforms={'on' if spec else 'off'} "
          f"mode={'replace' if args.replace else 'merge'} "
          f"progress={args.progress_key or 'off'}")

    if src_type in ("mysql", "postgresql"):
        print(f"  source TLS: {describe_source_tls(cfg['source'])}")
    else:
        print(f"  source TLS: as the {src_type} driver negotiates it (not verified "
              f"by the loader)")

    # Refuse a name that is not a plain identifier before touching the source,
    # with a message that names it, rather than failing inside the first query.
    try:
        sqlident.validate_qualified(table, "table")
    except sqlident.UnsafeIdentifier as exc:
        raise SystemExit(f"FATAL: {exc}")

    # 1) Discover PK bounds
    probe = ADAPTERS[src_type](cfg["source"])
    probe.connect()
    # pk from here on is the CHUNK column (the one the range is split by);
    # key_columns are what each row is keyed by. See resolve_keys.
    pk, key_columns = resolve_keys(probe, table, pk)
    try:
        sqlident.validate(pk, "chunk column")
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
    unverified = cdc_values.unverified_columns(src_type, columns)
    if unverified:
        print(f"  note: written as the driver prints them, which may differ from "
              f"CDC: {', '.join(unverified)}")
    names = {c.get("name") for c in columns}
    if VERSION_FIELD in names:
        print(f"  note: column {VERSION_FIELD} is not stored: that field holds the "
              f"row version the sink compares changes against (VERSION_FIELD)")
    absent = [c for c in [pk] + key_columns if c not in names]
    if absent:
        raise SystemExit(f"FATAL: {table} has no column {', '.join(absent)} (its "
                         f"columns: {', '.join(sorted(str(n) for n in names))}).")
    print(f"  rows keyed by {', '.join(key_columns)} (as CDC keys them: "
          f"{key_prefix}<{':'.join(sorted(key_columns))}>); split by {pk}")
    version = load_version_from_env()
    print(f"  row version ({VERSION_FIELD}): "
          + (f"{version} (capture paused at this broker time; older changes are "
             f"skipped, newer ones apply)" if version else
             "none (LOAD_VERSION unset): any later change applies over loaded rows"))
    lo, hi = probe.bounds(table, pk)
    if lo is None:
        print("Table is empty — nothing to load.")
        return
    check_chunk_bounds(table, pk, key_columns, lo, hi)
    # Optional watermark bounds: load only rows within [min-pk, max-pk]. Useful
    # for incremental loads (e.g. everything inserted after the last max PK).
    if args.min_pk is not None:
        lo = max(int(lo), args.min_pk)
    if args.max_pk is not None:
        hi = min(int(hi), args.max_pk)
    if int(lo) > int(hi):
        print(f"No rows in PK range [{args.min_pk}..{args.max_pk}] — nothing to load.")
        return
    # RESUME. The progress hash used to be wiped at the start of every run, so a
    # load that failed at 90% started again from nothing. If the previous run
    # in this table's own progress hash had the same identity (run_identity)
    # and did not complete, its chunks are reused: chunks it finished are
    # skipped and the rest restart from their last confirmed key.
    previous = {}
    if args.progress_key and args.resume:
        try:
            raw = target_client(cfg["target"]).hgetall(args.progress_key) or {}
            previous = {_as_str(k): _as_str(v) for k, v in raw.items()}
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (could not read previous progress, loading from the beginning: {exc})")
    identity = run_identity(table, key_prefix, spec, args.replace, pk, key_columns,
                            scope=f"{args.min_pk}..{args.max_pk}",
                            stack=os.environ.get("LOADER_STACK_ID", ""))
    stored = stored_chunks(previous, identity)
    row_count = table_rows(probe, table)
    if stored:
        ranges = extend_chunks(stored, lo, hi)
        plan = resume_plan(previous, len(ranges))
    else:
        ranges = split_ranges(probe, table, pk, lo, hi, workers, row_count)
        plan = {}
    print(f"PK range [{lo}..{hi}] split into {len(ranges)} chunks")
    if (previous and not stored and not previous.get("completed_at_ms")
            and _identity_differences(previous, identity)):
        print(f"  an incomplete earlier load in {args.progress_key} is not resumed: "
              f"it differs in {', '.join(_identity_differences(previous, identity))}; "
              f"loading from the first row")
    if plan:
        skipped = sum(1 for p in plan.values() if p == "done")
        added = len(ranges) - len(stored)
        print(f"  resuming an incomplete load of {table}: {skipped} of {len(ranges)} "
              f"chunk(s) already done; the rest continue after their last "
              f"confirmed key (--fresh to start over)"
              + (f"; {added} new chunk(s) for keys added since" if added else ""))

    # Before the first write, check that the load fits in the target memory.
    encoder = make_encoder(src_type, columns)
    try:
        sample = spread_sample(probe, table, pk, lo, hi, SAMPLE_ROWS)
    except Exception as exc:                                         # noqa: BLE001
        print(f"  (could not sample rows: {type(exc).__name__}: {exc})")
        sample = []
    check_fits(probe, target_client(cfg["target"]), table, pk, lo, key_prefix,
               spec, table_name, encode=encoder, key_columns=key_columns,
               hi=hi, rows=row_count, sample=sample, version=version)
    cached = (cached_share(target_client(cfg["target"]), sample, key_prefix,
                           key_columns, encoder) if not plan else 0)
    if cached:
        # Jira story F137: keys of a fresh load already present are an earlier load's, or
        # an earlier link's of the same name and key prefix.
        print(f"  note: {cached:.0%} of the sampled rows already have keys under "
              f"{key_prefix} (an earlier load, or an earlier link with this name). "
              + ("This load replaces them." if args.replace else
                 "This load merges into them: fields it does not write stay."))
    size_fetch_batch(sample, encoder, workers)
    # The probe is done. It used to stay open, idle, for the whole load.
    _close_quietly(getattr(probe, "conn", None))

    # 2) Load ranges in parallel
    start = time.time()

    tasks = []
    for i, r in enumerate(ranges):
        resume_at = plan.get(i)
        if resume_at == "done":
            continue
        chunk_lo = r[0] if resume_at is None else max(
            r[0], _after(resume_at, r[0], key_columns == [pk]))
        if chunk_lo > r[1]:
            continue
        tasks.append((src_type, cfg["source"], cfg["target"], table, pk, key_prefix,
                      chunk_lo, r[1], spec, table_name, i, args.progress_key,
                      args.replace, columns, tuple(key_columns)))
    # Seed the run header so a watcher can tell a fresh run from a stale one and
    # work out a percentage rather than just a rising count.
    if args.progress_key and plan:
        try:
            target_client(cfg["target"]).hset(args.progress_key, mapping={
                "resumed_at_ms": str(int(start * 1000)),
                "updated_at_ms": str(int(start * 1000)),
                "chunks": json.dumps(ranges), "workers": str(len(ranges))})
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (progress key unavailable: {exc})")
    elif args.progress_key:
        try:
            hdr = target_client(cfg["target"])
            hdr.delete(args.progress_key)
            hdr.hset(args.progress_key, mapping=dict(
                identity,
                # pk_lo, pk_hi and workers are for the dashboard; the chunks
                # are what a resumed run reuses.
                chunks=json.dumps(ranges),
                # The oldest version a row of this run carries: a resumed run
                # keeps the first run's, which the sweep compares against.
                load_version=version or "",
                pk_lo=str(lo), pk_hi=str(hi), workers=str(len(ranges)),
                started_at_ms=str(int(start * 1000)),
                updated_at_ms=str(int(start * 1000)),
                expected_rows=str(args.expected_rows or 0),
            ))
        except Exception as exc:                                     # noqa: BLE001
            print(f"  (progress key unavailable: {exc})")
    total = 0
    done = len(ranges) - len(tasks)
    nchunks = len(ranges)
    results = []

    def finished(result):
        nonlocal total, done
        results.append(result)
        total += result["written"]
        done += 1
        el = time.time() - start
        rate = total / el if el else 0
        print(f"  [{done:>2}/{nchunks} chunks done] {total:>12,} rows | {el:6.1f}s | {rate:>10,.0f} rows/sec",
              flush=True)

    ticker = ProgressTicker(cfg["target"], args.progress_key, nchunks, start)
    ticker.start()
    try:
        run_chunks(ProcessPoolExecutor(max_workers=workers), load_chunk, tasks, finished)
    except BrokenProcessPool as exc:
        # A worker killed from outside: almost always the kernel's OOM killer.
        mem = task_memory_bytes()
        raise SystemExit(
            f"FATAL: a loader worker process was killed while loading {table} "
            f"({exc}). That is almost always the task running out of memory: "
            f"{workers} workers x {FETCH_BATCH:,} rows per fetch"
            + (f" in {mem / 1048576:,.0f} MiB" if mem else "")
            + ". Lower advanced.loader.workers or advanced.loader.fetch_batch, or "
              "raise advanced.loader.memory. The load resumes where it stopped.")
    finally:
        ticker.stop()

    # Read back what was written before calling the load complete (D6).
    verify_and_repair(cfg["target"], tasks, results, load_version_from_env())

    # The sweep (Jira story F072): only after a complete replacing load of the whole key
    # range, with a version on every row of the run (a resumed run's rows from
    # before the resume carry the first run's version, which is the floor).
    floor = (previous.get("load_version") or None) if plan else version
    if args.replace and SWEEP:
        if args.min_pk is not None or args.max_pk is not None:
            print("  sweep skipped: the load covered part of the key range "
                  "(--min-pk/--max-pk)")
        elif not floor or (plan and not version):
            print("  sweep skipped: the rows of this load carry no version "
                  "(LOAD_VERSION unset)")
        else:
            vk = target_client(cfg["target"])
            try:
                gone, unversioned = sweep_stale(vk, key_prefix, floor, spec, table_name)
            finally:
                _close_quietly(vk)
            print(f"  sweep: {gone:,} key(s) under {key_prefix} older than this load "
                  f"deleted (rows the source no longer has)"
                  + (f"; {unversioned:,} key(s) without a version left as they are"
                     if unversioned else ""), flush=True)

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
