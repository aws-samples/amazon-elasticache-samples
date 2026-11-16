"""Write each value exactly as the CDC path writes it.

The cache is written by two programs: this loader (once, for the rows that
already exist) and the Go sink (for every change after that). The sink stores
what Debezium put in the change event, as text. The loader used to store
str(value) of whatever the database driver returned, so the same column came
out in two formats depending on which program wrote the row last:

    MySQL DATETIME   loader '2024-01-01 00:00:37'   CDC '1704067237000'
    MySQL TIMESTAMP  loader '2026-11-11 16:34:00'   CDC '2026-11-11T16:34:00Z'

A reader cannot parse a field that changes format when the row is updated, so
the loader now produces Debezium's representation. That is the only direction
that works: the sink receives schema-less JSON (schemas.enable=false keeps the
broker small) and cannot tell an epoch-milliseconds DATETIME from a BIGINT, so
it has nothing to convert from. The loader can read the column types from the
source catalogue, so it does the conversion.

The rules below are Debezium 3.0.6's defaults for the settings this repo uses
(data-plane/debezium-server/application.properties): time.precision.mode left
at the default (adaptive_time_microseconds for MySQL, adaptive elsewhere),
decimal.handling.mode=string, binary.handling.mode left at bytes (base64 in
JSON). Change any of those and this table must change with it;
tests/test_cdc_values.py pins the expected strings.

A value Debezium sends as a JSON number reaches the cache as that number's text,
which Java printed (Double.toString), so floating-point values are formatted
the Java way here.
"""
import base64
import datetime
import decimal
import json
import math
import struct

_EPOCH = datetime.datetime(1970, 1, 1)
_EPOCH_DATE = datetime.date(1970, 1, 1)
_UTC = datetime.timezone.utc


# ── value formatters ─────────────────────────────────────────────────────────

def _delta_micros(td):
    return (td.days * 86400 + td.seconds) * 1000000 + td.microseconds


def _naive(v):
    """A datetime without tzinfo, read as UTC (how Debezium reads zoneless types)."""
    if isinstance(v, datetime.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_UTC).replace(tzinfo=None)
        return v
    if isinstance(v, datetime.date):
        return datetime.datetime(v.year, v.month, v.day)
    raise TypeError('not a datetime: %r' % (v,))


def epoch_millis(v):
    # Floor, as Java's Instant.toEpochMilli does for times before 1970.
    return str(_delta_micros(_naive(v) - _EPOCH) // 1000)


def epoch_micros(v):
    return str(_delta_micros(_naive(v) - _EPOCH))


def epoch_nanos(v):
    return str(_delta_micros(_naive(v) - _EPOCH) * 1000)


def epoch_days(v):
    if isinstance(v, datetime.datetime):
        v = v.date()
    return str((v - _EPOCH_DATE).days)


def _time_micros(v):
    if isinstance(v, datetime.timedelta):     # pymysql returns TIME as timedelta
        return _delta_micros(v)
    if isinstance(v, datetime.time):
        return ((v.hour * 60 + v.minute) * 60 + v.second) * 1000000 + v.microsecond
    raise TypeError('not a time: %r' % (v,))


def time_millis(v):
    return str(_time_micros(v) // 1000)


def time_micros(v):
    return str(_time_micros(v))


def time_nanos(v):
    return str(_time_micros(v) * 1000)


def _fraction(micros, width):
    """Java appendFraction: `width` digits if fixed, else as few as needed (none for 0)."""
    digits = '%06d' % micros
    if width:
        return '.' + (digits + '000')[:width]
    digits = digits.rstrip('0')
    return '.' + digits if digits else ''


def iso_utc(v, width=None):
    """io.debezium.time.ZonedTimestamp in UTC: 2024-03-05T06:07:08.12Z.

    width=None is DateTimeFormatter.ISO_OFFSET_DATE_TIME (trailing zeros dropped);
    an integer 1..9 is Debezium's fixed-width formatter for the column's precision.
    """
    if isinstance(v, datetime.datetime) and v.tzinfo is not None:
        v = v.astimezone(_UTC)
    if width is not None and not 1 <= width <= 9:
        width = None
    return '%04d-%02d-%02dT%02d:%02d:%02d%sZ' % (
        v.year, v.month, v.day, v.hour, v.minute, v.second, _fraction(v.microsecond, width))


def iso_time_utc(v):
    """io.debezium.time.ZonedTime in UTC: 13:14:15.5Z."""
    if v.tzinfo is not None:
        off = v.utcoffset() or datetime.timedelta(0)
        base = datetime.datetime(2000, 1, 1, v.hour, v.minute, v.second, v.microsecond) - off
        v = base.time()
    return '%02d:%02d:%02d%sZ' % (v.hour, v.minute, v.second, _fraction(v.microsecond, None))


def boolean(v):
    if isinstance(v, (bytes, bytearray)):      # MySQL BIT(1) arrives as b'\x01'
        v = any(v)
    return 'true' if v else 'false'


def b64(v):
    return base64.b64encode(bytes(v)).decode('ascii')


def bits(v):
    """io.debezium.data.Bits: the bytes in little-endian order, then base64."""
    raw = bytes(v)
    return base64.b64encode(raw[::-1]).decode('ascii')


def decimal_text(v, scale=None):
    """BigDecimal.toPlainString(), at the column's scale when the driver dropped zeros."""
    if not isinstance(v, decimal.Decimal):
        v = decimal.Decimal(str(v))
    if not v.is_finite():
        return str(v)
    if scale is not None and scale >= 0 and -v.as_tuple().exponent < scale:
        v = v.quantize(decimal.Decimal(1).scaleb(-scale))
    return format(v, 'f')


def _java_style(digits, exp, negative):
    """Digits + decimal exponent -> Java's Double.toString/Float.toString layout."""
    sign = '-' if negative else ''
    point = exp + 1                     # position of the decimal point in digits
    if -3 < point <= 7:                 # 1e-3 <= |x| < 1e7: plain notation
        if point <= 0:
            return sign + '0.' + '0' * (-point) + digits
        if point >= len(digits):
            return sign + digits + '0' * (point - len(digits)) + '.0'
        return sign + digits[:point] + '.' + digits[point:]
    return sign + digits[0] + '.' + (digits[1:] or '0') + 'E' + str(exp)


def _shortest_digits(text):
    d = decimal.Decimal(text)
    sign, digs, _ = d.as_tuple()
    digits = ''.join(map(str, digs)).rstrip('0') or '0'
    return digits, d.adjusted(), bool(sign)


def java_double(v):
    x = float(v)
    if math.isnan(x):
        return 'NaN'
    if math.isinf(x):
        return 'Infinity' if x > 0 else '-Infinity'
    if x == 0:
        return '-0.0' if math.copysign(1, x) < 0 else '0.0'
    return _java_style(*_shortest_digits(repr(x)))


def _f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def java_float(v):
    """Float.toString: the shortest digits that round-trip as a 32-bit float."""
    x = float(v)
    if math.isnan(x) or math.isinf(x) or x == 0:
        return java_double(x)
    target = _f32(x)
    for p in range(1, 10):
        text = '%.*e' % (p - 1, target)
        if _f32(float(text)) == target:
            return _java_style(*_shortest_digits(text))
    return java_double(target)


def json_compact(v):
    """A list or dict as compact JSON with sorted keys, as Go's json.Marshal
    writes the nested values the sink receives (PostgreSQL arrays, structs)."""
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False, sort_keys=True,
                      default=str)


def json_text_compact(v):
    """MySQL JSON as Debezium sends it: the server's text without whitespace.

    SELECT returns '{"a": null, "b": [1, 2.5]}'; the binlog event decodes to
    '{"a":null,"b":[1,2.5]}' with the same key order. Only whitespace outside
    strings is removed, so numbers and strings keep their exact text.
    """
    if isinstance(v, (bytes, bytearray)):
        v = v.decode('utf-8')
    out, in_str, esc = [], False, False
    for ch in str(v):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch not in ' \t\r\n':
            out.append(ch)
    return ''.join(out)


def text(v):
    if isinstance(v, bool):
        return boolean(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return b64(v)
    if isinstance(v, decimal.Decimal):
        return decimal_text(v)
    if isinstance(v, float):
        return java_double(v)
    if isinstance(v, (list, dict)):
        return json_compact(v)
    return str(v)


# ── column type -> formatter ─────────────────────────────────────────────────

def _by_precision(p, millis, micros, nanos=None, default=6):
    p = default if p is None else int(p)
    if p <= 3:
        return millis
    if p <= 6 or nanos is None:
        return micros
    return nanos


def _decimal(scale):
    return lambda v: decimal_text(v, scale)


def _mysql(col):
    t, p, ct = col['type'], col.get('precision'), (col.get('column_type') or '').lower()
    if t == 'date':
        return epoch_days
    if t == 'time':
        return time_micros                  # adaptive_time_microseconds: always µs
    if t == 'datetime':
        return _by_precision(p, epoch_millis, epoch_micros, default=0)
    if t == 'timestamp':
        # MySQL TIMESTAMP is the one zoned type; Debezium prints the column's
        # fractional digits in full (TIMESTAMP(3) -> .100).
        width = int(p or 0)
        return lambda v: iso_utc(v, width or None)
    if t == 'bit':
        return boolean if ct in ('bit(1)', 'bit') else bits
    if t in ('binary', 'varbinary', 'blob', 'tinyblob', 'mediumblob', 'longblob'):
        return b64
    if t == 'json':
        return json_text_compact
    if t in ('decimal', 'numeric'):
        return _decimal(col.get('scale'))
    if t == 'float':
        return java_float
    if t in ('double', 'real'):
        return java_double
    return None


def _postgresql(col):
    t, p = col['type'], col.get('precision')
    if t == 'date':
        return epoch_days
    if t in ('time', 'time without time zone'):
        return _by_precision(p, time_millis, time_micros)
    if t in ('timetz', 'time with time zone'):
        return iso_time_utc
    if t in ('timestamp', 'timestamp without time zone'):
        return _by_precision(p, epoch_millis, epoch_micros)
    if t in ('timestamptz', 'timestamp with time zone'):
        return iso_utc
    if t in ('boolean', 'bool'):
        return boolean
    if t == 'bytea':
        return b64
    if t in ('numeric', 'decimal'):
        return _decimal(col.get('scale'))
    if t in ('real', 'float4'):
        return java_float
    if t in ('double precision', 'float8'):
        return java_double
    return None


def _sqlserver(col):
    t, p = col['type'], col.get('precision')
    if t == 'date':
        return epoch_days
    if t == 'time':
        return _by_precision(p, time_millis, time_micros, time_nanos, default=7)
    if t in ('datetime', 'smalldatetime'):
        return epoch_millis
    if t == 'datetime2':
        return _by_precision(p, epoch_millis, epoch_micros, epoch_nanos, default=7)
    if t == 'datetimeoffset':
        return iso_utc
    if t == 'bit':
        return boolean
    if t in ('binary', 'varbinary', 'image', 'timestamp', 'rowversion'):
        return b64
    if t in ('decimal', 'numeric', 'money', 'smallmoney'):
        return _decimal(col.get('scale'))
    if t == 'real':
        return java_float
    if t == 'float':
        return java_double
    if t == 'uniqueidentifier':
        return lambda v: str(v).upper()     # JDBC returns it upper case
    return None


def _oracle(col):
    t, p = col['type'], col.get('precision')
    if t == 'date':
        return epoch_millis                 # Oracle DATE carries a time of day
    if t.startswith('timestamp') and 'time zone' not in t:
        return _by_precision(p, epoch_millis, epoch_micros, epoch_nanos)
    if t.startswith('timestamp') and 'local time zone' in t:
        return iso_utc
    if t in ('raw', 'long raw', 'blob'):
        return b64
    if t == 'number':
        scale = col.get('scale')
        if scale is not None and int(scale) == 0 and col.get('precision') is not None:
            return lambda v: str(int(v))    # NUMBER(p,0): Debezium sends an integer
        return _decimal(None if scale is None else int(scale))
    if t == 'binary_float':
        return java_float
    if t == 'binary_double':
        return java_double
    return None


_ENGINES = {'mysql': _mysql, 'postgresql': _postgresql,
            'sqlserver': _sqlserver, 'oracle': _oracle}


def column_formatters(engine, columns):
    """{column name: formatter} for the columns that need more than text()."""
    pick = _ENGINES.get(engine)
    out = {}
    if not pick:
        return out
    for col in columns or []:
        fn = pick(dict(col, type=str(col.get('type') or '').lower()))
        if fn is not None:
            out[col['name']] = fn
    return out


def row_encoder(engine, columns):
    """A function row(dict) -> {field: text}, matching what CDC writes.

    NULL becomes the empty string, as it does in the sink (a Valkey hash cannot
    hold a null). Columns the catalogue did not describe fall back to text().
    """
    fmt = column_formatters(engine, columns)

    def encode(row):
        out = {}
        for k, v in row.items():
            if v is None:
                out[str(k)] = ''
                continue
            fn = fmt.get(k)
            out[str(k)] = fn(v) if fn is not None else text(v)
        return out
    return encode


def generic_encoder(row):
    """For a source whose column types could not be read."""
    return {str(k): ('' if v is None else text(v)) for k, v in row.items()}
