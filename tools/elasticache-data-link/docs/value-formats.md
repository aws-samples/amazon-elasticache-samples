# How column values are stored in the cache

Each row is a Valkey hash, and every field is text. Two programs write rows:
the bulk loader (the initial load) and the CDC sink (every change after it).
Both write a value in the same format, so a field keeps its format when a
loaded row is later updated by CDC.

The format is the one Debezium 3.0.6 puts in the change event, with the
connector settings in `data-plane/debezium-server/application.properties`.
The sink stores those events as they arrive, without schemas, so it cannot
convert them. The loader reads the column types from the source catalogue and
writes the same text (`data-plane/bulk-loader/cdc_values.py`).

| Column type | Stored as | Example |
|---|---|---|
| MySQL `DATETIME`, `DATETIME(1-3)`; SQL Server `datetime`, `smalldatetime`, `datetime2(0-3)`; PostgreSQL `timestamp(1-3)`; Oracle `DATE`, `TIMESTAMP(0-3)` | milliseconds since 1970-01-01, the value read as UTC | `1735689600000` |
| MySQL `DATETIME(4-6)`; PostgreSQL `timestamp` (default precision 6); SQL Server `datetime2(4-6)`; Oracle `TIMESTAMP(4-6)` | microseconds since 1970-01-01 | `1709618828123456` |
| SQL Server `datetime2(7)` (the default); Oracle `TIMESTAMP(7-9)` | nanoseconds since 1970-01-01 | `1000005000` |
| MySQL `TIMESTAMP(n)` | ISO 8601 in UTC, `n` fractional digits | `2026-11-11T16:34:00Z` |
| PostgreSQL `timestamptz`; SQL Server `datetimeoffset`; Oracle `TIMESTAMP WITH LOCAL TIME ZONE` | ISO 8601 in UTC, trailing zeros dropped | `2024-03-05T06:07:08.12Z` |
| `DATE` (all engines but Oracle) | days since 1970-01-01 | `19782` |
| `TIME` | microseconds since midnight (milliseconds for precision 0-3 outside MySQL; nanoseconds for SQL Server `time(7)`) | `47655000000` |
| `DECIMAL` / `NUMERIC` / Oracle `NUMBER(p,s)` | plain decimal at the column's scale | `12.50` |
| boolean, MySQL `BIT(1)` | `true` / `false` | `true` |
| binary (`VARBINARY`, `BLOB`, `bytea`, `RAW`) | base64 | `AP8Q` |
| `FLOAT`, `DOUBLE`, `REAL` | Java's number format | `1.23456789E7` |
| MySQL `JSON` | the server's JSON text without whitespace | `{"a":null,"b":[1,2.5]}` |
| PostgreSQL arrays | compact JSON | `[1,"a"]` |
| `NULL` | empty string | |

Limits. A NULL and an empty string are stored the same way. SQL Server
`datetime2(7)` and Oracle `TIMESTAMP(7-9)` keep only microseconds when loaded,
because the Python drivers return no more. MySQL `BIGINT UNSIGNED` above
2^63-1 is sent by Debezium as a negative number (`bigint.unsigned.handling.mode=long`).
Oracle `TIMESTAMP WITH TIME ZONE` and PostgreSQL `interval` are written as the
driver prints them and may differ from CDC.

Changing `time.precision.mode`, `decimal.handling.mode` or
`binary.handling.mode` in the Debezium configuration changes these formats for
CDC only. Change `cdc_values.py` and `tests/test_cdc_values.py` with it.
