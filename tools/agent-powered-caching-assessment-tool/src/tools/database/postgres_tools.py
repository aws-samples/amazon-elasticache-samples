"""
PostgreSQL Collector Tools

Every query runs through a command executor: on the database host over SSH
(SshPsqlExecutor), or locally (LocalPsqlExecutor). The queries read PostgreSQL's statistics
views (pg_stat_statements, pg_stat_user_tables, pg_stats, information_schema).

Each collection method issues ONE query for all collected tables. A per-table query would
cost a round trip per table, which does not scale to thousands of tables.
"""

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class PostgreSQLRemoteCollector:
    """Collects PostgreSQL metadata through a command executor."""

    def __init__(
        self,
        executor,
        host: str,
        port: int,
        database: str,
        schemas: list[str] | None = None,
    ):
        """
        Args:
            executor: Command executor. Any object exposing run_sql, run_sql_json and
                run_command works (SshPsqlExecutor or LocalPsqlExecutor).
            host: Database host, as seen from where the executor runs psql.
            port: Database port.
            database: Database name.
            schemas: Schemas to collect from. When None, call
                discover_schemas() and pass the result to set_schemas().
                Defaults to ["public"] only so a caller that never discovers
                still behaves predictably.
        """
        self.executor = executor
        self.host = host
        self.port = port
        self.database = database
        self.schemas = schemas or ["public"]

    def set_schemas(self, schemas: list[str]) -> None:
        """Set the schemas all subsequent collection calls will target.

        Args:
            schemas: Schema names. Ignored when empty, so a failed discovery
                cannot silently reduce collection to nothing.
        """
        if schemas:
            self.schemas = schemas
            logger.info("Collecting from schemas: %s", ", ".join(schemas))

    @property
    def _schema_list(self) -> str:
        """Current schemas as a SQL IN clause."""
        return _schema_in_clause(self.schemas)

    def _query(self, sql: str) -> list[dict]:
        return self.executor.run_sql_json(
            host=self.host, port=self.port, database=self.database, sql=sql)

    def _query_raw(self, sql: str) -> str:
        return self.executor.run_sql(
            host=self.host, port=self.port, database=self.database, sql=sql)

    # -------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------

    def get_version(self) -> str:
        rows = self._query("SELECT version() AS version")
        return str(rows[0]["version"]) if rows else "unknown"

    def get_database_size_gb(self) -> float:
        rows = self._query(
            "SELECT pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0 AS size_gb"
        )
        return float(rows[0]["size_gb"] or 0) if rows else 0

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def discover_schemas(self) -> list[dict]:
        """Find every non-system schema containing user tables.

        Called before collection so the tool works against a database whose
        schema layout is unknown. pg_stat_user_tables already excludes
        pg_catalog and information_schema, so no exclusion list is needed.

        Returns:
            List of {"schema_name": str, "table_count": int}, busiest first.
            Empty list if the database has no user tables.
        """
        return self._query("""
            SELECT schemaname AS schema_name, count(*) AS table_count
            FROM pg_stat_user_tables
            GROUP BY schemaname
            ORDER BY table_count DESC, schemaname
        """)

    def collect_tables(self) -> list[dict]:
        """Collect table metadata across the configured schemas.

        Sizes use a regclass cast over quote_ident rather than raw string
        concatenation, so mixed-case and reserved-word identifiers resolve.

        Returns:
            List of table metadata dicts.
        """
        return self._query(f"""
            SELECT
                schemaname AS schema_name,
                relname AS table_name,
                n_live_tup AS row_count,
                pg_relation_size(
                    (quote_ident(schemaname) || '.' || quote_ident(relname))::regclass
                ) / 1024.0 / 1024.0 AS data_size_mb,
                pg_indexes_size(
                    (quote_ident(schemaname) || '.' || quote_ident(relname))::regclass
                ) / 1024.0 / 1024.0 AS index_size_mb,
                seq_scan AS full_table_scans,
                idx_scan AS index_scans,
                -- Planner row estimate. Unlike n_live_tup it is not zeroed by
                -- pg_stat_reset(), so it stays usable after a stats reset.
                -- -1 means the table was never analyzed or vacuumed.
                -- A scalar subquery rather than a join, so relname stays
                -- unambiguous.
                (SELECT reltuples FROM pg_class WHERE oid = relid) AS reltuples
            FROM pg_stat_user_tables
            WHERE schemaname IN {self._schema_list}
            ORDER BY schemaname, relname
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def _resolve_schema(self, schema_name: str | None) -> str:
        """Pick the schema to scope a per-table query to.

        Args:
            schema_name: Explicit schema, or None to use the first configured one.

        Returns:
            Schema name with single quotes escaped for SQL embedding.
        """
        name = schema_name or (self.schemas[0] if self.schemas else "public")
        return name.replace("'", "''")

    def collect_columns_all(self) -> list[dict]:
        """Collect column metadata for every collected table, in one query.

        Views are left out: only tables that pg_stat_user_tables lists are returned.

        Returns:
            One dict per column: schema_name, table_name, column_name, ordinal_position,
            data_type, udt_name, max_length, is_nullable, column_default.
        """
        return self._query(f"""
            SELECT
                c.table_schema AS schema_name, c.table_name,
                c.column_name, c.ordinal_position, c.data_type, c.udt_name,
                c.character_maximum_length AS max_length,
                c.is_nullable, c.column_default
            FROM information_schema.columns c
            JOIN pg_stat_user_tables t
              ON t.schemaname = c.table_schema AND t.relname = c.table_name
            WHERE c.table_schema IN {self._schema_list}
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def collect_column_stats(self) -> list[dict]:
        """Collect planner column statistics for every collected table, in one query.

        n_distinct is returned raw. A negative value is a fraction of the row
        count and is converted in analysis, where reltuples is available, so the
        collected value stays auditable. Columns of a table that was never
        analyzed have no pg_stats row.

        Returns:
            One dict per column: schema_name, table_name, column_name,
            n_distinct, avg_width.
        """
        return self._query(f"""
            SELECT
                schemaname AS schema_name,
                tablename AS table_name,
                attname AS column_name,
                n_distinct,
                avg_width
            FROM pg_stats
            WHERE schemaname IN {self._schema_list}
              AND NOT inherited
            ORDER BY schemaname, tablename, attname
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def collect_column_widths(self) -> list[dict]:
        """Collect the declared storage width of every column, in one query.

        Used by sizing, so a cached value has a byte width even for a column
        pg_stats has no row for (a table never analyzed, or a column added
        since). typlen is the fixed width in bytes, or -1 / -2 for a variable
        width type. atttypmod carries the declared limit of a varchar(n) or
        char(n) as n + 4, and -1 when there is none. Reads the catalog only,
        never row contents.

        Returns:
            One dict per column: schema_name, table_name, column_name, typlen,
            atttypmod, type_name, not_null.
        """
        return self._query(f"""
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name,
                t.typlen AS typlen,
                a.atttypmod AS atttypmod,
                format_type(a.atttypid, a.atttypmod) AS type_name,
                a.attnotnull AS not_null
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE n.nspname IN {self._schema_list}
              AND c.relkind IN ('r', 'p')
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def collect_indexes(self, table_name: str, schema_name: str | None = None) -> list[dict]:
        """Collect index definitions for one table.

        Args:
            table_name: Table to inspect.
            schema_name: Schema containing it.

        Returns:
            List of index dicts with columns grouped per index.
        """
        schema = self._resolve_schema(schema_name)
        safe_table = table_name.replace("'", "''")
        raw = self._query(f"""
            SELECT
                i.relname AS index_name,
                a.attname AS column_name,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary,
                am.amname AS index_type
            FROM pg_class t
            JOIN pg_index ix ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
            JOIN pg_am am ON i.relam = am.oid
            WHERE t.relname = '{safe_table}'
              AND t.relnamespace = (
                  SELECT oid FROM pg_namespace WHERE nspname = '{schema}'
              )
            ORDER BY i.relname, a.attnum
        """)  # nosec B608 — identifiers escaped above
        indexes: dict[str, dict] = {}
        for r in raw:
            name = r["index_name"]
            if name not in indexes:
                indexes[name] = {
                    "index_name": name,
                    "columns": [],
                    "is_unique": r.get("is_unique") in (True, "t", "true"),
                    "is_primary": r.get("is_primary") in (True, "t", "true"),
                    "index_type": str(r.get("index_type") or "btree").lower(),
                }
            indexes[name]["columns"].append(r["column_name"])
        return list(indexes.values())

    def collect_foreign_keys(
        self, table_name: str, schema_name: str | None = None
    ) -> list[dict]:
        """Collect foreign key constraints for one table.

        Args:
            table_name: Table to inspect.
            schema_name: Schema containing it.

        Returns:
            List of FK dicts with columns grouped per constraint.
        """
        schema = self._resolve_schema(schema_name)
        safe_table = table_name.replace("'", "''")
        raw = self._query(f"""
            SELECT
                tc.constraint_name, kcu.column_name,
                ccu.table_name AS referenced_table_name,
                ccu.column_name AS referenced_column_name,
                rc.update_rule AS on_update, rc.delete_rule AS on_delete
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = '{schema}' AND tc.table_name = '{safe_table}'
            ORDER BY tc.constraint_name
        """)  # nosec B608 — identifiers escaped above
        fks: dict[str, dict] = {}
        for r in raw:
            name = r["constraint_name"]
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table": r["referenced_table_name"],
                    "referenced_columns": [],
                    "on_delete": r.get("on_delete"),
                    "on_update": r.get("on_update"),
                }
            fks[name]["columns"].append(r["column_name"])
            fks[name]["referenced_columns"].append(r["referenced_column_name"])
        return list(fks.values())

    def collect_primary_keys_all(self) -> list[dict]:
        """Collect primary key columns for every collected table, in one query.

        Returns:
            One dict per key column: schema_name, table_name, column_name, position (1 is
            the first column of the key). A table with no primary key has no rows.
        """
        return self._query(f"""
            SELECT n.nspname AS schema_name, c.relname AS table_name,
                   a.attname AS column_name, k.pos AS position
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, pos)
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE i.indisprimary
              AND n.nspname IN {self._schema_list}
            ORDER BY n.nspname, c.relname, k.pos
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self) -> list[dict]:
        """Collect view definitions across the configured schemas."""
        return self._query(f"""
            SELECT schemaname AS schema_name, viewname AS view_name,
                   definition, viewowner AS owner
            FROM pg_views
            WHERE schemaname IN {self._schema_list}
            ORDER BY schemaname, viewname
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def collect_procedures(self) -> list[dict]:
        """Collect function and procedure definitions across the schemas."""
        return self._query(f"""
            SELECT
                p.proname AS routine_name,
                CASE p.prokind WHEN 'f' THEN 'FUNCTION' WHEN 'p' THEN 'PROCEDURE' END AS routine_type,
                pg_get_function_result(p.oid) AS return_type,
                l.lanname AS language
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            JOIN pg_language l ON p.prolang = l.oid
            WHERE n.nspname IN {self._schema_list}
            ORDER BY routine_type, routine_name
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    def collect_triggers(self) -> list[dict]:
        """Collect trigger definitions across the configured schemas."""
        return self._query(f"""
            SELECT
                t.tgname AS trigger_name,
                c.relname AS table_name,
                CASE t.tgtype & 2 WHEN 2 THEN 'BEFORE' ELSE 'AFTER' END AS timing,
                CASE t.tgtype & 28
                    WHEN 4 THEN 'INSERT' WHEN 8 THEN 'DELETE'
                    WHEN 16 THEN 'UPDATE' ELSE 'MULTIPLE'
                END AS event_type,
                CASE t.tgtype & 1 WHEN 1 THEN 'ROW' ELSE 'STATEMENT' END AS for_each
            FROM pg_trigger t
            JOIN pg_class c ON t.tgrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname IN {self._schema_list} AND NOT t.tgisinternal
            ORDER BY c.relname, t.tgname
        """)  # nosec B608 — schema names escaped by _schema_in_clause

    # -------------------------------------------------------------------
    # Query patterns (pg_stat_statements)
    # -------------------------------------------------------------------

    def count_query_patterns(self, min_calls: int = 1) -> int:
        """How many query patterns the database holds that pass the call-count filter.

        Used to say how many a LIMIT cut off.

        Args:
            min_calls: Ignore patterns with fewer cumulative calls than this.

        Returns:
            The count.
        """
        min_calls = int(min_calls)
        rows = self._query(f"""
            SELECT count(*) AS n
            FROM pg_stat_statements
            WHERE calls >= {min_calls}
              AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
        """)  # nosec B608 — min_calls is an int
        return int(rows[0]["n"]) if rows else 0

    def collect_query_patterns(self, min_calls: int = 1, limit: int = 1000) -> list[dict]:
        min_calls, limit = int(min_calls), int(limit)
        raw = self._query(f"""
            SELECT
                queryid,
                -- Collapse all whitespace so the query text is one line. Application SQL
                -- is normally multi-line, and a single-line text is easier to read in
                -- a report and to match with the regular expressions below.
                regexp_replace(query, '\\s+', ' ', 'g') AS query,
                calls,
                total_exec_time AS total_time_ms,
                mean_exec_time AS avg_time_ms,
                min_exec_time AS min_time_ms,
                max_exec_time AS max_time_ms,
                stddev_exec_time AS stddev_time_ms,
                rows,
                shared_blks_hit, shared_blks_read,
                blk_read_time AS io_read_time_ms,
                blk_write_time AS io_write_time_ms,
                temp_blks_read, temp_blks_written
            FROM pg_stat_statements
            WHERE calls >= {min_calls}
              AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            ORDER BY total_exec_time DESC
            LIMIT {limit}
        """)  # nosec B608 — min_calls and limit are ints

        # pg_stat_statements counters are cumulative since the last reset, so a
        # rate requires knowing how long they have been accumulating. This was
        # previously hardcoded to 24 hours, which understated the rate by ~700x
        # against a freshly reset window and overstated it on a long-running
        # server. Rates drive Access Suitability (35% of confidence) and the
        # min_calls_per_sec hard exclusion, so the error decided verdicts.
        window_seconds = self._stats_window_seconds()

        patterns = []
        for r in raw:
            query_text = str(r.get("query") or "")
            calls = r.get("calls") or 1
            avg_ms = float(r.get("avg_time_ms") or 0)
            stddev_ms = float(r.get("stddev_time_ms") or 0)
            shared_hit = int(r.get("shared_blks_hit") or 0)
            shared_read = int(r.get("shared_blks_read") or 0)
            total_blks = shared_hit + shared_read

            p50, p95, p99 = _estimate_percentiles(avg_ms, stddev_ms)

            patterns.append(
                {
                    "query_id": str(r.get("queryid") or _hash(query_text)),
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "execution_count": calls,
                    "frequency_per_hour": round(calls / (window_seconds / 3600.0), 3),
                    "calls_per_second": round(calls / window_seconds, 6),
                    "stats_window_seconds": round(window_seconds, 1),
                    "execution_time_ms_avg": avg_ms,
                    "execution_time_ms_stddev": stddev_ms,
                    "execution_time_ms_min": float(r.get("min_time_ms") or 0),
                    "execution_time_ms_max": float(r.get("max_time_ms") or 0),
                    "execution_time_ms_p50": p50,
                    "execution_time_ms_p95": p95,
                    "execution_time_ms_p99": p99,
                    "total_time_ms": float(r.get("total_time_ms") or 0),
                    "rows_returned_avg": (r.get("rows") or 0) / calls,
                    "rows_returned_p95": _estimate_rows_p95(
                        (r.get("rows") or 0) / calls, avg_ms, p95
                    ),
                    "rows_affected_avg": 0,
                    "rows_examined_avg": total_blks
                    * 8192.0
                    / max(calls, 1),  # blocks * 8KB as proxy for rows examined
                    "scan_efficiency_pct": (
                        min(
                            round((r.get("rows") or 0) / max(total_blks * 8192.0, 1) * 100, 2),
                            100.0,
                        )
                        if total_blks > 0
                        else None
                    ),
                    "tables_accessed": _extract_tables(query_text),
                    "filter_columns": _extract_filter_columns(query_text),
                    "sort_columns": _extract_sort_columns(query_text),
                    "has_joins": " join " in query_text.lower(),
                    "join_count": len(re.findall(r"\bjoin\b", query_text, re.I)),
                    "has_aggregations": bool(
                        re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                    ),
                    "has_subqueries": query_text.lower().count("select") > 1,
                    "has_text_search": _has_text_search(query_text),
                    "text_search_type": _text_search_type(query_text),
                    "has_time_range_filter": _has_time_range_filter(query_text),
                    # PostgreSQL-specific
                    "cache_hit_ratio_pct": round(shared_hit / max(total_blks, 1) * 100, 2),
                    "shared_blks_hit": shared_hit,
                    "shared_blks_read": shared_read,
                    "io_read_time_ms": float(r.get("io_read_time_ms") or 0),
                    "io_write_time_ms": float(r.get("io_write_time_ms") or 0),
                    "temp_blocks_read": int(r.get("temp_blks_read") or 0),
                    "temp_blocks_written": int(r.get("temp_blks_written") or 0),
                }
            )
        return patterns

    # -------------------------------------------------------------------
    # Global stats
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        rows = self._query("""
            SELECT
                numbackends AS active_connections,
                xact_commit + xact_rollback AS total_transactions,
                blks_hit, blks_read,
                tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                temp_files, temp_bytes,
                EXTRACT(EPOCH FROM stats_reset) AS stats_reset_epoch,
                EXTRACT(EPOCH FROM (now() - COALESCE(stats_reset, pg_postmaster_start_time())))
                    AS stats_window_seconds
            FROM pg_stat_database WHERE datname = current_database()
        """)
        if not rows:
            return {}
        r = rows[0]
        blks_hit = int(r.get("blks_hit") or 0)
        blks_read = int(r.get("blks_read") or 0)
        return {
            "cache_hit_ratio_pct": round(blks_hit / max(blks_hit + blks_read, 1) * 100, 2),
            "buffer_pool_hits": blks_hit,
            "buffer_pool_reads_from_disk": blks_read,
            "buffer_pool_read_requests": blks_hit + blks_read,
            "tmp_disk_tables": int(r.get("temp_files") or 0),
            "tmp_tables": int(r.get("temp_files") or 0),
            "temp_bytes": int(float(r.get("temp_bytes") or 0)),
            "active_connections": int(r.get("active_connections") or 0),
            "total_transactions": int(r.get("total_transactions") or 0),
            # The period these counters (and pg_stat_user_tables) cover, so a count can
            # become a rate. pg_stat_reset() resets both.
            "stats_reset_epoch": _float_or_none(r.get("stats_reset_epoch")),
            "stats_window_seconds": _float_or_none(r.get("stats_window_seconds")),
        }

    # -------------------------------------------------------------------
    # Collection window: counters at the start and the end of the window
    # -------------------------------------------------------------------

    def database_clock(self) -> float:
        """The database server's clock, as Unix seconds. Window lengths use it."""
        rows = self._query("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS epoch")
        return float(rows[0]["epoch"])

    def snapshot_statement_counters(self) -> dict[str, dict]:
        """Every pg_stat_statements entry's cumulative counters, without query text.

        Taken at the start of a collection window. Rows for the same queryid under
        different users are summed, so a query is one entry. The sum of squared
        execution times is rebuilt from the mean and the population standard
        deviation, so the window's own deviation can be derived from two snapshots.

        Returns:
            queryid -> counters (see statement_counters_from_rows).
        """
        rows = self._query("""
            SELECT queryid, calls,
                   total_exec_time AS total_time_ms,
                   mean_exec_time AS avg_time_ms,
                   stddev_exec_time AS stddev_time_ms,
                   rows, shared_blks_hit, shared_blks_read,
                   blk_read_time AS io_read_time_ms,
                   blk_write_time AS io_write_time_ms,
                   temp_blks_read, temp_blks_written
            FROM pg_stat_statements
            WHERE queryid IS NOT NULL
              AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
        """)
        return statement_counters_from_rows(rows, id_key="queryid")

    def statements_info(self) -> dict:
        """pg_stat_statements_info (PostgreSQL 14+): evictions and the reset time.

        Returns:
            {"dealloc": int, "stats_reset_epoch": float | None}. Raises before PG 14.
        """
        rows = self._query("""
            SELECT dealloc, EXTRACT(EPOCH FROM stats_reset) AS stats_reset_epoch
            FROM pg_stat_statements_info
        """)
        if not rows:
            return {}
        return {"dealloc": int(float(rows[0].get("dealloc") or 0)),
                "stats_reset_epoch": _float_or_none(rows[0].get("stats_reset_epoch"))}

    # -------------------------------------------------------------------
    # Caching-assessment additions
    #
    # These close the gaps identified in docs/apcat/04-deterministic-scoring.md. None of
    # these were collected by the upstream migration tool.
    # -------------------------------------------------------------------

    def _stats_window_seconds(self, default: float = 3600.0) -> float:
        """Seconds over which pg_stat_statements counters have accumulated.

        Every per-query rate depends on this. The counters are cumulative since
        the last reset, so dividing by a fixed assumed window produces a rate
        that is wrong by whatever factor the real window differs.

        Sources tried in order:
          1. pg_stat_statements_info.stats_reset (PostgreSQL 14+), the exact
             reset time for these specific counters.
          2. pg_stat_database.stats_reset, which tracks a different reset but is
             usually close on a normally operated server.
          3. pg_postmaster_start_time(), correct when nothing was ever reset.

        Args:
            default: Fallback when every source fails, in seconds.

        Returns:
            Window in seconds, floored at 1.0 to avoid division by zero on a
            reset that happened moments ago.
        """
        try:
            rows = self._query("""
                SELECT GREATEST(
                    EXTRACT(EPOCH FROM (now() - COALESCE(
                        (SELECT stats_reset FROM pg_stat_statements_info),
                        (SELECT stats_reset FROM pg_stat_database
                          WHERE datname = current_database()),
                        pg_postmaster_start_time()
                    ))), 1.0
                ) AS window_seconds
            """)
            if rows and rows[0].get("window_seconds"):
                return max(float(rows[0]["window_seconds"]), 1.0)
        except Exception:
            # pg_stat_statements_info does not exist before PostgreSQL 14, and
            # the COALESCE cannot rescue a missing relation.
            logger.warning("Could not determine stats window; trying fallback")

        try:
            rows = self._query("""
                SELECT GREATEST(
                    EXTRACT(EPOCH FROM (now() - COALESCE(
                        (SELECT stats_reset FROM pg_stat_database
                          WHERE datname = current_database()),
                        pg_postmaster_start_time()
                    ))), 1.0
                ) AS window_seconds
            """)
            if rows and rows[0].get("window_seconds"):
                return max(float(rows[0]["window_seconds"]), 1.0)
        except Exception:
            logger.warning(
                "Could not determine stats window at all; assuming %.0fs. "
                "Per-query rates will be unreliable.", default
            )
        return default

    def probe_pg_stat_statements(self) -> dict:
        """Check whether pg_stat_statements is available before relying on it.

        The extension is not enabled by default on self-managed PostgreSQL: it
        must be present in shared_preload_libraries (requires a server restart)
        AND created in the target database. Without it, every query-level signal
        in the scoring model is unavailable and the assessment must fall back to
        DEGRADED mode (table + global + host signals only).

        Returns:
            {"available": bool, "in_preload": bool, "extension_installed": bool,
             "reason": str | None}
        """
        rows = self._query("""
            SELECT
                (SELECT setting FROM pg_settings
                  WHERE name = 'shared_preload_libraries') AS preload,
                EXISTS (SELECT 1 FROM pg_extension
                         WHERE extname = 'pg_stat_statements') AS ext_installed
        """)
        if not rows:
            return {
                "available": False,
                "in_preload": False,
                "extension_installed": False,
                "reason": "could not query pg_settings / pg_extension",
            }

        preload = str(rows[0].get("preload") or "")
        in_preload = "pg_stat_statements" in preload
        # psql prints a boolean as t or f, and bool("f") is True.
        ext = str(rows[0].get("ext_installed")).lower() in ("t", "true", "1")

        reason = None
        if not in_preload:
            reason = (
                "pg_stat_statements not in shared_preload_libraries — "
                "requires a postgresql.conf change and a server RESTART"
            )
        elif not ext:
            reason = (
                "library preloaded but extension not created in this database — "
                "run CREATE EXTENSION pg_stat_statements (no restart needed)"
            )

        return {
            "available": in_preload and ext,
            "in_preload": in_preload,
            "extension_installed": ext,
            "reason": reason,
        }

    def collect_relevant_config(self) -> dict:
        """Memory/planner settings needed to interpret buffer-pressure signals.

        Context for Pressure Evidence scoring: a low buffer hit ratio means
        something different when shared_buffers is 128MB versus 32GB.
        """
        rows = self._query("""
            SELECT name, setting, unit
            FROM pg_settings
            WHERE name IN (
                'shared_buffers', 'effective_cache_size', 'work_mem',
                'maintenance_work_mem', 'max_connections',
                'random_page_cost', 'seq_page_cost'
            )
        """)
        return {
            str(r["name"]): {"setting": r.get("setting"), "unit": r.get("unit")}
            for r in rows
        }

    def collect_bgwriter_stats(self) -> dict:
        """Checkpoint and buffer-write activity from pg_stat_bgwriter.

        A high ratio of buffers_backend to buffers_checkpoint indicates backends
        are flushing their own dirty buffers — classic evidence that the working
        set does not fit in shared_buffers.

        Note: PostgreSQL 17 split this view into pg_stat_bgwriter and
        pg_stat_checkpointer. This method targets PG 13-16 column names; add a
        version branch before running against PG 17+.
        """
        rows = self._query("""
            SELECT
                checkpoints_timed, checkpoints_req,
                checkpoint_write_time, checkpoint_sync_time,
                buffers_checkpoint, buffers_clean, buffers_backend,
                buffers_backend_fsync, buffers_alloc, maxwritten_clean,
                EXTRACT(EPOCH FROM stats_reset) AS stats_reset_epoch,
                EXTRACT(EPOCH FROM (now() - COALESCE(stats_reset, pg_postmaster_start_time())))
                    AS window_seconds
            FROM pg_stat_bgwriter
        """)
        if not rows:
            return {}
        r = rows[0]
        return bgwriter_summary(r)

    def collect_wait_events(self) -> list[dict]:
        """Current wait-event distribution from pg_stat_activity.

        Feeds the Disqualifiers dimension: if slowness is dominated by Lock
        waits, a cache will not fix it. This is a point-in-time sample, not a
        cumulative counter — call repeatedly if a distribution is needed.
        """
        return self._query("""
            SELECT
                COALESCE(wait_event_type, 'None') AS wait_event_type,
                COALESCE(wait_event, 'None') AS wait_event,
                state,
                count(*) AS session_count
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
            GROUP BY 1, 2, 3
            ORDER BY session_count DESC
        """)

    def collect_lock_contention(self) -> list[dict]:
        """Blocked/blocking session pairs from pg_locks.

        Non-empty results are a Disqualifier signal: lock contention is not
        solved by adding a cache.
        """
        return self._query("""
            SELECT
                blocked.pid AS blocked_pid,
                blocked_act.query AS blocked_query,
                blocking.pid AS blocking_pid,
                blocking_act.query AS blocking_query,
                blocked.locktype,
                blocked.mode AS blocked_mode,
                -- The relation by OID, so the lock penalty never depends on
                -- parsing query text. Null for non-relation locks.
                (SELECT n.nspname || '.' || c.relname
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE c.oid = blocked.relation) AS relation_name
            FROM pg_locks blocked
            JOIN pg_stat_activity blocked_act ON blocked_act.pid = blocked.pid
            JOIN pg_locks blocking
              ON blocking.locktype = blocked.locktype
             AND blocking.granted
             AND blocking.pid <> blocked.pid
             AND COALESCE(blocking.relation, 0) = COALESCE(blocked.relation, 0)
            JOIN pg_stat_activity blocking_act ON blocking_act.pid = blocking.pid
            WHERE NOT blocked.granted
        """)

    def collect_table_churn(self) -> list[dict]:
        """Dead-tuple and vacuum activity per table.

        High dead-tuple churn marks a volatile table whose cached values would
        be invalidated frequently — a Disqualifier input. Also returns the raw
        insert/update/delete counters needed for a true table-level read:write
        ratio (the upstream tool only derived this from query patterns).
        """
        return self._query("""
            SELECT
                schemaname AS schema_name,
                relname AS table_name,
                n_live_tup, n_dead_tup,
                n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd,
                seq_scan, seq_tup_read, idx_scan, idx_tup_fetch,
                last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
            FROM pg_stat_user_tables
            ORDER BY (n_tup_ins + n_tup_upd + n_tup_del) DESC
        """)

    # NOTE: collect_sample_data was removed deliberately. A caching assessment
    # needs access statistics, not row contents. Not reading customer data also
    # removes the PII-handling and anonymization burden the upstream migration
    # tool had to carry. Do not reintroduce it without a concrete requirement.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUERY_TYPE_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.I)

# Captures an optional schema qualifier and the table name separately.
#
# The previous pattern stripped only a literal "public." prefix, so against
# "FROM retail.orders" it captured "retail" (the schema) instead of "orders".
# That silently broke the join between query patterns and tables: every table
# looked like it had no traffic. Validated in
# tests/manual/test_collector_fixes_standalone.py.
_TABLE_RE = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+(?:\"?(\w+)\"?\s*\.\s*)?\"?(\w+)\"?",
    re.I,
)

# Tokens that can follow FROM/JOIN but are not table names.
_NOT_TABLE_TOKENS = frozenset(
    {"select", "lateral", "unnest", "values", "only", "generate_series"}
)


def _schema_in_clause(schemas: list[str]) -> str:
    """Build a SQL IN clause from schema names.

    Schema names originate from pg_stat_user_tables or a CLI argument, so
    embedded single quotes are doubled to prevent SQL injection.

    Args:
        schemas: Schema names to include.

    Returns:
        A parenthesised IN list such as "('retail','public')". Returns
        "(NULL)" for an empty input so the query matches nothing rather than
        everything.
    """
    if not schemas:
        return "(NULL)"
    return "(" + ",".join(f"'{s.replace(chr(39), chr(39) * 2)}'" for s in schemas) + ")"


def resolve_table_ids(raw_names: list[str], known_table_ids: list[str]) -> list[str]:
    """Map extracted table references onto canonical schema.table ids.

    Queries often omit the schema because search_path supplies it, so bare
    names must be matched against the collected table list. Without this step
    query patterns never join to their tables and every table scores as though
    it received no traffic.

    Args:
        raw_names: Output of _extract_tables, qualified or bare.
        known_table_ids: Canonical "schema.table" ids from collection.

    Returns:
        Resolved ids in first-seen order. Names that cannot be resolved
        unambiguously are passed through unchanged rather than dropped, so the
        information is not silently lost.
    """
    by_bare: dict[str, list[str]] = {}
    for tid in known_table_ids:
        by_bare.setdefault(tid.rsplit(".", 1)[-1].lower(), []).append(tid)
    known_lower = {t.lower(): t for t in known_table_ids}

    resolved: list[str] = []
    for name in raw_names:
        exact = known_lower.get(name.lower())
        if exact:
            if exact not in resolved:
                resolved.append(exact)
            continue
        candidates = by_bare.get(name.lower(), [])
        if len(candidates) == 1:
            # Unambiguous bare name.
            if candidates[0] not in resolved:
                resolved.append(candidates[0])
        elif name not in resolved:
            # Either unknown, or ambiguous across schemas. Do not guess.
            resolved.append(name)
    return resolved
_WHERE_COL_RE = re.compile(r"WHERE\s+.*?\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_AND_COL_RE = re.compile(r"(?:AND|OR)\s+\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_ORDER_COL_RE = re.compile(r"ORDER\s+BY\s+(.*?)(?:LIMIT|OFFSET|$)", re.I)


def _extract_query_type(sql: str) -> str:
    m = _QUERY_TYPE_RE.match(sql)
    return m.group(1).upper() if m else "OTHER"


def _extract_tables(sql: str) -> list[str]:
    """Extract table references from query text, keeping schema qualification.

    Args:
        sql: Query text, normalised or raw.

    Returns:
        Unique references in first-seen order, qualified as "schema.table" when
        the query qualified them and bare otherwise. Returns ["unknown"] when
        nothing could be extracted, since downstream code requires a non-empty
        tables_accessed list.
    """
    found: list[str] = []
    for schema, table in _TABLE_RE.findall(sql or ""):
        if table.lower() in _NOT_TABLE_TOKENS:
            continue
        name = f"{schema}.{table}" if schema else table
        if name not in found:
            found.append(name)
    return found or ["unknown"]


def _extract_filter_columns(sql: str) -> list[str] | None:
    cols = _WHERE_COL_RE.findall(sql) + _AND_COL_RE.findall(sql)
    return list(dict.fromkeys(cols)) or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    m = _ORDER_COL_RE.search(sql)
    if not m:
        return None
    raw = m.group(1)
    cols = [c.strip().strip('"').split()[0] for c in raw.split(",")]
    return [c for c in cols if c and c.upper() not in ("ASC", "DESC")] or None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _estimate_percentiles(mean: float, stddev: float) -> tuple[float, float, float]:
    """Estimate p50/p95/p99 from mean+stddev (normal approximation)."""
    p50 = max(0.0, mean)
    p95 = max(0.0, mean + 1.645 * stddev)
    p99 = max(0.0, mean + 2.326 * stddev)
    return round(p50, 3), round(p95, 3), round(p99, 3)


def _estimate_rows_p95(avg_rows: float, avg_ms: float, p95_ms: float) -> float | None:
    if not avg_ms or avg_ms == 0 or not p95_ms:
        return None
    return round(avg_rows * (p95_ms / avg_ms), 1)


def _has_text_search(query_text: str) -> bool:
    if re.search(r"\b(to_tsvector|to_tsquery|plainto_tsquery|@@)\b", query_text, re.I):
        return True
    if re.search(r"\s%\s", query_text):
        return True
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return True
    # pg_stat_statements parameterizes: LIKE $1
    if re.search(r"\b[I]?LIKE\s+\$\d+", query_text, re.I):
        return True
    return False


def _text_search_type(query_text: str) -> str | None:
    if re.search(r"\b(to_tsvector|to_tsquery|@@)\b", query_text, re.I):
        return "tsvector"
    if re.search(r"\s%\s", query_text):
        return "trigram"
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return "like_wildcard"
    if re.search(r"\b[I]?LIKE\s+\$\d+", query_text, re.I):
        return "like_wildcard"
    return None


def _has_time_range_filter(query_text: str) -> bool:
    if re.search(
        r"\b(now\(\)|CURRENT_TIMESTAMP|CURRENT_DATE)\s*[-+]\s*interval\b", query_text, re.I
    ):
        return True
    if re.search(r"\bBETWEEN\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    if re.search(r"[<>]=?\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    # pg_stat_statements: now() - $1 or date column > $1
    if re.search(r"\b(now\(\)|CURRENT_TIMESTAMP)\s*[-+]\s*\$\d+", query_text, re.I):
        return True
    if re.search(
        r"\b(date|time|created|updated|timestamp)\w*\b\s*[<>]=?\s*\$\d+", query_text, re.I
    ):
        return True
    return False


def _float_or_none(value: Any) -> float | None:
    """A number from psql output, or None when empty or not a number."""
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# The cumulative counters a collection window subtracts. sumsq_ms is the sum of squared
# execution times, rebuilt from mean and standard deviation because PostgreSQL does not
# expose it.
STATEMENT_COUNTERS = ("calls", "total_time_ms", "sumsq_ms", "rows", "shared_blks_hit",
                      "shared_blks_read", "io_read_time_ms", "io_write_time_ms",
                      "temp_blks_read", "temp_blks_written")


def statement_counters_from_rows(rows: list[dict], id_key: str = "queryid") -> dict[str, dict]:
    """Cumulative pg_stat_statements counters per query id, summed across users.

    Args:
        rows: Rows with calls, total_time_ms, avg_time_ms, stddev_time_ms, rows,
            shared_blks_hit, shared_blks_read, io_read_time_ms, io_write_time_ms,
            temp_blks_read and temp_blks_written. Query patterns from
            collect_query_patterns() work too, with id_key="query_id" (their field
            names are mapped).
        id_key: The field holding the query id.

    Returns:
        query id (text) -> {counter: float} for every name in STATEMENT_COUNTERS.
    """
    out: dict[str, dict] = {}
    for r in rows or []:
        qid = r.get(id_key)
        if qid in (None, ""):
            continue
        calls = _float_or_none(r.get("calls", r.get("execution_count"))) or 0.0
        mean = _float_or_none(r.get("avg_time_ms", r.get("execution_time_ms_avg"))) or 0.0
        sd = _float_or_none(r.get("stddev_time_ms", r.get("execution_time_ms_stddev"))) or 0.0
        total = _float_or_none(r.get("total_time_ms")) or 0.0
        rows_ = r.get("rows")
        if rows_ is None and r.get("rows_returned_avg") is not None:
            rows_ = float(r["rows_returned_avg"]) * calls
        vals = {
            "calls": calls,
            "total_time_ms": total,
            "sumsq_ms": (sd * sd + mean * mean) * calls,
            "rows": _float_or_none(rows_) or 0.0,
            "shared_blks_hit": _float_or_none(r.get("shared_blks_hit")) or 0.0,
            "shared_blks_read": _float_or_none(r.get("shared_blks_read")) or 0.0,
            "io_read_time_ms": _float_or_none(r.get("io_read_time_ms")) or 0.0,
            "io_write_time_ms": _float_or_none(r.get("io_write_time_ms")) or 0.0,
            "temp_blks_read": _float_or_none(r.get("temp_blks_read", r.get("temp_blocks_read"))) or 0.0,
            "temp_blks_written": _float_or_none(
                r.get("temp_blks_written", r.get("temp_blocks_written"))) or 0.0,
        }
        acc = out.setdefault(str(qid), dict.fromkeys(STATEMENT_COUNTERS, 0.0))
        for k, v in vals.items():
            acc[k] += v
    return out


def bgwriter_summary(r: dict) -> dict:
    """pg_stat_bgwriter counters with the derived percentages.

    Args:
        r: One row of pg_stat_bgwriter counters, or window differences of them, with
            an optional window_seconds and stats_reset_epoch.

    Returns:
        The counters as numbers, buffers_backend_pct, requested_checkpoint_pct and
        window_seconds (None when unknown).
    """
    out: dict = {}
    for k, v in r.items():
        out[k] = _float_or_none(v) if k not in ("stats_reset",) else v
        if isinstance(out[k], float) and out[k].is_integer() and k not in (
                "window_seconds", "stats_reset_epoch"):
            out[k] = int(out[k])
    bg = int(out.get("buffers_backend") or 0)
    ckpt = int(out.get("buffers_checkpoint") or 0)
    clean = int(out.get("buffers_clean") or 0)
    req = int(out.get("checkpoints_req") or 0)
    timed = int(out.get("checkpoints_timed") or 0)
    total = bg + ckpt + clean
    out["buffers_backend_pct"] = round(bg / max(total, 1) * 100, 2)
    out["requested_checkpoint_pct"] = round(req / max(req + timed, 1) * 100, 2)
    out["window_seconds"] = _float_or_none(out.get("window_seconds"))
    return out
