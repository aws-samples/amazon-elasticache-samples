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
| PostgreSQL `timestamptz(n)` (default 6) | ISO 8601 in UTC, `n` fractional digits (none for 0) | `2024-03-05T06:07:08.120000Z` |
| SQL Server `datetimeoffset` | ISO 8601 at the value's own offset, trailing zeros dropped | `2024-03-05T11:37:08.12+05:30` |
| Oracle `TIMESTAMP WITH LOCAL TIME ZONE` | ISO 8601 in UTC, trailing zeros dropped | `2024-03-05T06:07:08.12Z` |
| MySQL zero date (`0000-00-00`, or a zero month or day) in `DATE` / `DATETIME` | empty (NULL) if the column is nullable, else `0` | |
| MySQL zero `TIMESTAMP` | the epoch | `1970-01-01T00:00:00Z` |
| MySQL `BIGINT UNSIGNED` | the 64 bits as a signed number: 2^63 and above are negative | `-1` for 18446744073709551615 |
| PostgreSQL `bit(1)` | `true` / `false` | `true` |
| PostgreSQL `bit(n)`, `varbit` | the bits as a little-endian number, base64, no leading zero bytes (all zeros is empty) | `BQ==` for `101` |
| PostgreSQL `money` | plain decimal | `-1234.56` |
| PostgreSQL `interval` | microseconds, a month counted as 30.4375 days | `2629800000000` for `1 mon` |
| PostgreSQL `hstore` | JSON object, in Java `HashMap` order | `{"a":"1","b":null}` |
| PostgreSQL `date[]`, `time[]`, `timestamp[]`, `timestamptz[]`, `timetz[]` | JSON array, each element as its type above | `[19782,null]` |
| PostgreSQL `money[]`, `interval[]`, `varbit[]` | not stored (CDC leaves the field out) | |
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
2^63-1 is sent by Debezium as a negative number (`bigint.unsigned.handling.mode=long`),
and the loader writes it the same way, including in keys. Setting the mode to
`precise` would not help: with these settings Debezium then sends every
`BIGINT UNSIGNED` value, and key, as base64 bytes. A NOT NULL MySQL zero date
whose column has a non-zero `DEFAULT` is written as the epoch; CDC may send the
default instead. Oracle `TIMESTAMP WITH TIME ZONE` and PostgreSQL types not
listed here (for example `point` or PostGIS) are written as the driver prints
them and may differ from CDC; the loader log names such columns
(`note: written as the driver prints them`).

The rules that are not in Debezium's documentation (the `timestamptz` width,
`datetimeoffset`, `interval`, `hstore`, `money`, `bit`) were checked against
the Debezium 3.0.6 source. `data-plane/bulk-loader/tests/reference/DebeziumRef.java`
runs those code paths on a JVM; the expected values in `tests/test_cdc_values.py`
come from it.

Changing `time.precision.mode`, `decimal.handling.mode` or
`binary.handling.mode` in the Debezium configuration changes these formats for
CDC only. Change `cdc_values.py` and `tests/test_cdc_values.py` with it.

## Computed fields and filters

The sink and the loader apply a link's transforms with the same rules, so a
computed field has the same text whichever wrote the row. Both are tested
against `tools/go-sink/testdata/transform_cases.json`.

- An expression uses `+ - * /`, parentheses, unary minus, numbers (`12`,
  `1.5`, `.5`, `1e3`, `2.5E-1`) and column names (`[A-Za-z_][A-Za-z0-9_]*`).
  Anything else (`%`, `**`, a function call, `0x10`, `1_000`, parentheses and
  signs nested more than 64 deep) is refused when the link is validated, and
  the sink and the loader refuse to start with it.
- A column is read as a number when, after trimming ASCII spaces, it is a
  plain decimal with an optional sign and exponent, or `NaN`, `Inf` or
  `Infinity` in any case with an optional sign. Anything else, and a missing
  column, reads as 0. Numeric filter operators (`> >= < <=`) read both sides
  the same way, and a comparison with NaN is false; `=` and `!=` compare text.
- Division by zero gives 0. Arithmetic is double precision, so an overflow
  gives Infinity.
- A result is written with up to six decimals and no trailing zeros (`1250`,
  `0.333333`), `-0` as `0`, and NaN and infinities as `NaN`, `Infinity` and
  `-Infinity`, the way a `FLOAT` or `DOUBLE` column is written.
