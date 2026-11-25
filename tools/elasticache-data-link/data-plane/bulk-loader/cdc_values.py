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

Where a rule below is not documented behaviour it names the Debezium 3.0.6
(or pgjdbc 42.7) source it follows: those cases were checked against the code
that produces the change event, not against a live event.
"""
import base64
import datetime
import decimal
import json
import math
import re
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


# A field Debezium leaves out of the change event (a type it has no schema
# for). The loader leaves it out too, so a loaded row has the fields a CDC
# write gives it.
OMIT = object()


def omitted(v):
    """The formatter of a column Debezium leaves out, NULL or not."""
    return OMIT


def iso_offset(v):
    """A datetime in its own UTC offset, ISO_OFFSET_DATE_TIME: 2024-03-05T11:37:08.12+05:30.

    SQL Server datetimeoffset: SqlServerValueConverters.convertTimestampWithZone
    builds an OffsetDateTime at the value's own offset, and ZonedTimestamp
    formats an OffsetDateTime with ISO_OFFSET_DATE_TIME (as many fractional
    digits as needed, none for 0; 'Z' for +00:00). A value without tzinfo is
    read as UTC.
    """
    off = v.utcoffset() if isinstance(v, datetime.datetime) and v.tzinfo else None
    total = int(off.total_seconds()) if off is not None else 0
    if total == 0:
        zone = 'Z'
    else:
        sign = '+' if total > 0 else '-'
        h, rest = divmod(abs(total), 3600)
        m, sec = divmod(rest, 60)
        zone = '%s%02d:%02d' % (sign, h, m) + (':%02d' % sec if sec else '')
    return '%04d-%02d-%02dT%02d:%02d:%02d%s%s' % (
        v.year, v.month, v.day, v.hour, v.minute, v.second,
        _fraction(v.microsecond, None), zone)


# MySQL zero dates. pymysql returns '0000-00-00' (or '2024-00-15') as a string,
# which no date formatter can read, so one such value used to fail the whole
# table. Debezium's binlog reader turns a DATE or DATETIME with a zero year,
# month or day into NULL (RowDeserializers.deserializeDate / deserializeDatetime),
# and JdbcValueConverters.convertValue sends NULL for a nullable column and the
# epoch for a NOT NULL one -- the documented zero-value rule. A TIMESTAMP zero
# is stored as 0 seconds, so the binlog has the epoch either way.
_ZERO_DATE = re.compile(r'\s*(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?\Z')


def _mysql_zero_date(fn, epoch_text, nullable, always_epoch=False):
    def fmt(v):
        if isinstance(v, (bytes, bytearray)):
            v = v.decode('ascii', 'replace')
        if not isinstance(v, str):
            return fn(v)
        m = _ZERO_DATE.match(v)
        if m and 0 in (int(m.group(1)), int(m.group(2)), int(m.group(3))):
            return epoch_text if (always_epoch or not nullable) else ''
        # Anything else is an invalid date (ALLOW_INVALID_DATES), which
        # Debezium's default event.processing.failure.handling.mode=fail
        # refuses as well.
        raise ValueError('MySQL date value %r cannot be read as a date' % (v,))
    return fmt


def unsigned_bigint(v):
    """MySQL BIGINT UNSIGNED as Debezium sends it with
    bigint.unsigned.handling.mode=precise (application-mysql.properties): the
    exact value. It used to copy the default, long, which sends the 64 bits as
    a signed long, so 18446744073709551615 was cached as -1 (review finding M9)."""
    return str(int(v))


def pg_bits(v):
    """PostgreSQL bit(n>1) / varbit: io.debezium.data.Bits, base64.

    PostgresValueConverter.convertBits puts the last character in bit 0 and
    sends BitSet.toByteArray(): little-endian, only as many bytes as the
    highest set bit needs (none for all zeros, which is the empty string).
    """
    n = int(str(v), 2) if str(v) else 0
    return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, 'little')).decode('ascii')


def pg_bit1(v):
    """PostgreSQL bit(1): a boolean (convertBit: Integer.valueOf(text, 2) != 0)."""
    return 'true' if int(str(v) or '0', 2) else 'false'


def pg_money(v):
    """PostgreSQL money with decimal.handling.mode=string.

    The value is the server's text ('$1,234.56'). AbstractColumnValue.asMoney
    drops its first character (the currency symbol) and the commas, reads a
    leading '-' as negative, and BigDecimal.toPlainString() prints the result:
    '1234.56', '-1234.56'. Text that does not parse that way fails CDC too.
    """
    text = str(v)
    negative = text.startswith('-') or text.startswith('(')
    body = text.lstrip('-').strip('()')[1:].replace(',', '')
    try:
        amount = decimal.Decimal(body)
    except decimal.InvalidOperation:
        raise ValueError('PostgreSQL money value %r is not $-style text (lc_monetary); '
                         'Debezium cannot read it either' % (text,))
    return ('-' if negative else '') + format(amount, 'f')


def _java_round(x):
    return int(math.floor(x + 0.5))


def pg_interval(v):
    """PostgreSQL interval as io.debezium.time.MicroDuration (interval.handling.mode
    left at numeric): microseconds, months counted as 365.25 / 12 days.

    The text ('1 year 2 mons 3 days 04:05:06.789', IntervalStyle postgres) is
    parsed the way pgjdbc's PGInterval parses it, and the microseconds are
    computed with MicroDuration.durationMicros's double arithmetic, so the
    rounding is Java's too.
    """
    text = str(v)
    years = months = days = hours = minutes = 0
    seconds = 0.0
    value = None
    for i, token in enumerate(text.replace('+', ' ').replace('@', ' ').lower().split(), 1):
        if i % 2 == 1:
            colon = token.find(':')
            if colon == -1:
                value = token
                continue
            neg = token.startswith('-')
            off = 1 if neg else 0
            hours = int(token[off:colon])
            minutes = int(token[colon + 1:colon + 3])
            second_colon = token.find(':', colon + 1)
            if second_colon != -1:
                seconds = float(token[second_colon + 1:])
            if neg:
                hours, minutes, seconds = -hours, -minutes, -seconds
            value = None
        else:
            n = 0 if value is None else value
            if token.startswith('year'):
                years = int(n)
            elif token.startswith('mon'):
                months = int(n)
            elif token.startswith('day'):
                days = int(n)
            elif token.startswith('hour'):
                hours = int(n)
            elif token.startswith('min'):
                minutes = int(n)
            elif token.startswith('sec'):
                seconds = float(n)
    # PGInterval.setSeconds / getSeconds: whole seconds and rounded micros.
    whole = int(seconds)
    micros = _java_round((seconds - whole) * 1000000)
    seconds = whole + micros / 1000000.0
    number_of_days = ((years * 12) + months) * (365.25 / 12.0) + days
    number_of_seconds = (((number_of_days * 24 + hours) * 60) + minutes) * 60 + seconds
    return str(_java_long_cast(number_of_seconds * 1e6))


def _java_long_cast(x):
    """Java's (long) of a double: truncated, and saturated at the long range."""
    if math.isnan(x):
        return 0
    if x >= 9223372036854775807.0:
        return 9223372036854775807
    if x <= -9223372036854775808.0:
        return -9223372036854775808
    return int(x)


def _java_hash(text):
    h = 0
    raw = text.encode('utf-16-le')
    for i in range(0, len(raw), 2):
        h = (31 * h + (raw[i] | raw[i + 1] << 8)) & 0xFFFFFFFF
    return h


def _jackson_string(text):
    """A JSON string as Jackson writes it: control characters as \\uXXXX with
    upper-case hex (\\b \\t \\n \\f \\r short), nothing else escaped."""
    short = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\t': '\\t', '\n': '\\n',
             '\f': '\\f', '\r': '\\r'}
    out = []
    for ch in text:
        if ch in short:
            out.append(short[ch])
        elif ord(ch) < 0x20:
            out.append('\\u%04X' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def _hstore_pairs(text):
    """pgjdbc HStoreConverter.fromString: (key, value or None) in text order."""
    pairs, pos, s = [], 0, text

    def until_quote(start):
        buf, i = [], start + 1
        while i < len(s):
            ch = s[i]
            if ch == '"':
                break
            if ch == '\\':
                i += 1
                ch = s[i]
            buf.append(ch)
            i += 1
        return ''.join(buf), i
    while pos < len(s):
        start = s.find('"', pos)
        if start == -1:
            break
        key, end = until_quote(start)
        pos = end + 3
        if s[pos] == 'N':
            val = None
            pos += 4
        else:
            val, pos = until_quote(pos)
        pos += 1
        pairs.append((key, val))
    return pairs


def pg_hstore(v):
    """PostgreSQL hstore with hstore.handling.mode left at json.

    PostgresValueConverter writes the map HStoreConverter.fromString returns
    (a java.util.HashMap) with Jackson, in the map's iteration order. That
    order is reproduced: HashMap bucket order for its final table size, insertion
    order within a bucket. (A bucket of 8 or more colliding keys is turned into
    a tree in Java, which this does not model.)
    """
    entries = {}
    for key, val in _hstore_pairs(str(v)):
        entries[key] = val                       # a repeated key keeps its place
    cap = 16
    while len(entries) > cap * 3 // 4:
        cap *= 2

    def bucket(key):
        h = _java_hash(key)
        return (h ^ (h >> 16)) & (cap - 1)
    order = sorted(enumerate(entries.items()), key=lambda e: (bucket(e[1][0]), e[0]))
    return '{' + ','.join('%s:%s' % (_jackson_string(k), 'null' if val is None
                                                       else _jackson_string(val))
                          for _, (k, val) in order) + '}'


def pg_array(element, numeric):
    """A PostgreSQL array of a temporal type: each element as that type is sent
    (PostgresValueConverter.createArrayConverter applies the element type's
    converter), as compact JSON. `numeric` elements are JSON numbers."""
    def fmt(v):
        if isinstance(v, str):
            raise ValueError('PostgreSQL array arrived as text %r' % (v,))
        parts = []
        for item in v:
            if item is None:
                parts.append('null')
            elif isinstance(item, list):
                parts.append(fmt(item))
            else:
                text = element(item)
                parts.append(text if numeric else json.dumps(text))
        return '[' + ','.join(parts) + ']'
    return fmt


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
    nullable = str(col.get('nullable', 'YES')).upper() != 'NO'
    if t == 'date':
        return _mysql_zero_date(epoch_days, '0', nullable)
    if t == 'time':
        return time_micros                  # adaptive_time_microseconds: always µs
    if t == 'datetime':
        return _mysql_zero_date(_by_precision(p, epoch_millis, epoch_micros, default=0),
                                '0', nullable)
    if t == 'timestamp':
        # MySQL TIMESTAMP is the one zoned type; Debezium prints the column's
        # fractional digits in full (TIMESTAMP(3) -> .100).
        width = int(p or 0)
        zoned = lambda v: iso_utc(v, width or None)          # noqa: E731
        return _mysql_zero_date(zoned, zoned(_EPOCH), nullable, always_epoch=True)
    if t == 'bigint' and 'unsigned' in ct:
        return unsigned_bigint
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


# Element formatters for PostgreSQL arrays, by udt_name: (formatter for a
# precision, elements are JSON numbers). Arrays Debezium has no schema for are
# left out of the event (schemaBuilder returns null for them).
def _pg_array_element(udt, p):
    if udt == '_date':
        return epoch_days, True
    if udt == '_time':
        return _by_precision(p, time_millis, time_micros), True
    if udt == '_timetz':
        return iso_time_utc, False
    if udt == '_timestamp':
        return _by_precision(p, epoch_millis, epoch_micros), True
    if udt == '_timestamptz':
        return (lambda v: iso_utc(v, 6 if p is None else int(p) or None)), False
    return None, None


_PG_OMITTED_ARRAYS = ('_money', '_interval', '_varbit')


def _pg_typmod_precision(col):
    """The declared precision of an array of a temporal type (atttypmod), or None."""
    mod = col.get('typmod')
    return None if mod is None or int(mod) < 0 else int(mod)


def _postgresql(col):
    t, p = col['type'], col.get('precision')
    udt = str(col.get('udt') or '').lower()
    if t == 'array':
        if udt in _PG_OMITTED_ARRAYS:
            return omitted
        element, numeric = _pg_array_element(udt, _pg_typmod_precision(col))
        return pg_array(element, numeric) if element else None
    if udt == 'hstore':
        return pg_hstore
    if t == 'bit' or t == 'bit varying':
        return pg_bit1 if col.get('length') == 1 else pg_bits
    if t == 'money':
        return pg_money
    if t == 'interval':
        return pg_interval
    if t == 'date':
        return epoch_days
    if t in ('time', 'time without time zone'):
        return _by_precision(p, time_millis, time_micros)
    if t in ('timetz', 'time with time zone'):
        return iso_time_utc
    if t in ('timestamp', 'timestamp without time zone'):
        return _by_precision(p, epoch_millis, epoch_micros)
    if t in ('timestamptz', 'timestamp with time zone'):
        # Fixed width: PostgresValueConverter.convertTimestampWithZone passes
        # the column's scale, which pgjdbc gives as the precision (6 unless
        # declared; TypeInfoCache.getScale), so '...08.120000Z'. Precision 0
        # uses the plain ISO format.
        width = 6 if p is None else int(p)
        return lambda v: iso_utc(v, width or None)
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
        return iso_offset
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


# PostgreSQL types whose driver text is what Debezium sends (or that psycopg2
# returns as a value text() prints the same way). Anything else that has no
# formatter is named in the loader's log, so a format nobody checked is seen.
_PG_PLAIN = {
    'smallint', 'integer', 'bigint', 'text', 'character varying', 'character',
    '"char"', 'name', 'uuid', 'json', 'jsonb', 'oid', 'inet', 'cidr', 'macaddr',
    'macaddr8', 'int4range', 'int8range', 'numrange', 'tsrange', 'tstzrange',
    'daterange', 'xml'}
_PG_PLAIN_UDT = {'citext', 'ltree', '_int2', '_int4', '_int8', '_text', '_varchar',
                 '_bpchar', '_bool', '_uuid', '_float8', '_float4', '_numeric'}


def unverified_columns(engine, columns):
    """'name (type)' of PostgreSQL columns with no checked format."""
    if engine != 'postgresql':
        return []
    fmt = column_formatters(engine, columns)
    out = []
    for col in columns or []:
        t = str(col.get('type') or '').lower()
        udt = str(col.get('udt') or '').lower()
        if col['name'] in fmt or t in _PG_PLAIN or udt in _PG_PLAIN_UDT:
            continue
        out.append('%s (%s)' % (col['name'], udt or t))
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
            fn = fmt.get(k)
            if fn is omitted:
                continue
            if v is None:
                out[str(k)] = ''
                continue
            value = fn(v) if fn is not None else text(v)
            if value is not OMIT:
                out[str(k)] = value
        return out
    return encode


def generic_encoder(row):
    """For a source whose column types could not be read."""
    return {str(k): ('' if v is None else text(v)) for k, v in row.items()}
