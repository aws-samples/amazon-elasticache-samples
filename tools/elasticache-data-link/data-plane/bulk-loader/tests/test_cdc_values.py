"""The loader writes every value in the format the CDC path writes it.

Expected strings are what Debezium 3.0.6 + the Go sink store, with this repo's
connector settings; see docs/value-formats.md. The DATETIME and TIMESTAMP(0)
cases are the values the end-to-end test read back from a live pipeline.
"""
import datetime
import decimal
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cdc_values as cv  # noqa: E402

D = decimal.Decimal
UTC = datetime.timezone.utc


def enc(engine, columns, row):
    return cv.row_encoder(engine, columns)(row)


class MySQLTest(unittest.TestCase):
    COLS = [
        {'name': 'created', 'type': 'datetime', 'precision': 0},
        {'name': 'dt3', 'type': 'datetime', 'precision': 3},
        {'name': 'dt6', 'type': 'datetime', 'precision': 6},
        {'name': 'changed_at', 'type': 'timestamp', 'precision': 0},
        {'name': 'ts3', 'type': 'timestamp', 'precision': 3},
        {'name': 'd', 'type': 'date'},
        {'name': 't', 'type': 'time', 'precision': 0},
        {'name': 'b1', 'type': 'bit', 'column_type': 'bit(1)'},
        {'name': 'b16', 'type': 'bit', 'column_type': 'bit(16)'},
        {'name': 'bin', 'type': 'varbinary'},
        {'name': 'price', 'type': 'decimal', 'scale': 4},
        {'name': 'dbl', 'type': 'double'},
        {'name': 'qty', 'type': 'int'},
        {'name': 'name', 'type': 'varchar'},
    ]

    def test_e2e_defect_values(self):
        # The two formats the end-to-end test found disagreeing.
        out = enc('mysql', self.COLS, {
            'created': datetime.datetime(2025, 1, 1, 0, 0),
            'changed_at': datetime.datetime(2026, 11, 11, 16, 34, 0)})
        self.assertEqual(out['created'], '1735689600000')
        self.assertEqual(out['changed_at'], '2026-11-11T16:34:00Z')

    def test_temporal(self):
        out = enc('mysql', self.COLS, {
            'dt3': datetime.datetime(2024, 3, 5, 6, 7, 8, 120000),
            'dt6': datetime.datetime(2024, 3, 5, 6, 7, 8, 123456),
            'ts3': datetime.datetime(2024, 3, 5, 6, 7, 8, 100000),
            'd': datetime.date(1969, 12, 31),
            't': datetime.timedelta(hours=13, minutes=14, seconds=15)})
        self.assertEqual(out['dt3'], '1709618828120')
        self.assertEqual(out['dt6'], '1709618828123456')
        self.assertEqual(out['ts3'], '2024-03-05T06:07:08.100Z')
        self.assertEqual(out['d'], '-1')
        self.assertEqual(out['t'], str((13 * 3600 + 14 * 60 + 15) * 1000000))

    def test_before_epoch_floors(self):
        out = enc('mysql', self.COLS, {'dt3': datetime.datetime(1969, 12, 31, 23, 59, 59, 999500)})
        self.assertEqual(out['dt3'], '-1')

    def test_other_types(self):
        out = enc('mysql', self.COLS, {
            'b1': b'\x01', 'b16': b'\x01\x02', 'bin': b'\x00\xff\x10',
            'price': D('12.3400'), 'dbl': 12345678.9, 'qty': None, 'name': "O'Brien"})
        self.assertEqual(out['b1'], 'true')
        self.assertEqual(out['b16'], 'AgE=')          # little-endian, then base64
        self.assertEqual(out['bin'], 'AP8Q')
        self.assertEqual(out['price'], '12.3400')
        self.assertEqual(out['dbl'], '1.23456789E7')
        self.assertEqual(out['qty'], '')
        self.assertEqual(out['name'], "O'Brien")

    def test_values_read_back_from_live_cdc(self):
        # Captured from the e2e pipeline (Debezium 3.0.6 -> sink -> cache).
        cols = [{'name': 'ts6', 'type': 'timestamp', 'precision': 6},
                {'name': 'ts3', 'type': 'timestamp', 'precision': 3},
                {'name': 'js', 'type': 'json'}, {'name': 'f', 'type': 'float'},
                {'name': 'b16', 'type': 'bit', 'column_type': 'bit(16)'},
                {'name': 'dt3', 'type': 'datetime', 'precision': 3},
                {'name': 't6', 'type': 'time', 'precision': 6}]
        out = enc('mysql', cols, {
            'ts6': datetime.datetime(2024, 3, 5, 6, 7, 8, 123400),
            'ts3': datetime.datetime(2024, 3, 5, 6, 7, 8),
            'js': '{"a": null, "b": [1, 2.5, "x y"]}', 'f': 0.000123, 'b16': b'\x00\x01',
            'dt3': datetime.datetime(1960, 1, 1, 0, 0, 0, 1000),
            't6': datetime.timedelta(seconds=86399, microseconds=999999)})
        self.assertEqual(out, {'ts6': '2024-03-05T06:07:08.123400Z',
                               'ts3': '2024-03-05T06:07:08.000Z',
                               'js': '{"a":null,"b":[1,2.5,"x y"]}', 'f': '1.23E-4',
                               'b16': 'AQA=', 'dt3': '-315619199999', 't6': '86399999999'})


class PostgreSQLTest(unittest.TestCase):
    COLS = [
        {'name': 'ts', 'type': 'timestamp without time zone', 'precision': 6},
        {'name': 'ts3', 'type': 'timestamp without time zone', 'precision': 3},
        {'name': 'tz', 'type': 'timestamp with time zone', 'precision': 6},
        {'name': 'tm', 'type': 'time without time zone', 'precision': 6},
        {'name': 'd', 'type': 'date'},
        {'name': 'ok', 'type': 'boolean'},
        {'name': 'raw', 'type': 'bytea'},
        {'name': 'amt', 'type': 'numeric', 'scale': 2},
        {'name': 'tags', 'type': 'ARRAY'},
    ]

    def test_values(self):
        out = enc('postgresql', self.COLS, {
            'ts': datetime.datetime(2024, 3, 5, 6, 7, 8, 123456),
            'ts3': datetime.datetime(2024, 3, 5, 6, 7, 8, 120000),
            'tz': datetime.datetime(2024, 3, 5, 8, 7, 8, 120000,
                                    tzinfo=datetime.timezone(datetime.timedelta(hours=2))),
            'tm': datetime.time(1, 2, 3, 4),
            'd': datetime.date(2024, 2, 29),
            'ok': True, 'raw': memoryview(b'ab'), 'amt': D('5'), 'tags': [1, 'a']})
        self.assertEqual(out['ts'], '1709618828123456')
        self.assertEqual(out['ts3'], '1709618828120')
        # Jira story F076: fixed width, the column's precision (was '...08.12Z').
        self.assertEqual(out['tz'], '2024-03-05T06:07:08.120000Z')
        self.assertEqual(out['tm'], '3723000004')
        self.assertEqual(out['d'], '19782')
        self.assertEqual(out['ok'], 'true')
        self.assertEqual(out['raw'], 'YWI=')
        self.assertEqual(out['amt'], '5.00')
        self.assertEqual(out['tags'], '[1,"a"]')


class SQLServerOracleTest(unittest.TestCase):
    def test_sqlserver(self):
        cols = [{'name': 'dt2', 'type': 'datetime2', 'precision': 7},
                {'name': 'dt', 'type': 'datetime'},
                {'name': 'off', 'type': 'datetimeoffset', 'precision': 7},
                {'name': 'flag', 'type': 'bit'},
                {'name': 'g', 'type': 'uniqueidentifier'}]
        out = enc('sqlserver', cols, {
            'dt2': datetime.datetime(1970, 1, 1, 0, 0, 1, 5),
            'dt': datetime.datetime(1970, 1, 1, 0, 0, 1, 997000),
            'off': datetime.datetime(2024, 1, 1, tzinfo=UTC),
            'flag': False, 'g': 'abc-def'})
        self.assertEqual(out['dt2'], '1000005000')
        self.assertEqual(out['dt'], '1997')
        self.assertEqual(out['off'], '2024-01-01T00:00:00Z')    # +00:00 is Z
        self.assertEqual(out['flag'], 'false')
        self.assertEqual(out['g'], 'ABC-DEF')

    def test_oracle(self):
        cols = [{'name': 'CREATED', 'type': 'DATE'},
                {'name': 'TS', 'type': 'timestamp', 'precision': 6},
                {'name': 'AMOUNT', 'type': 'NUMBER', 'precision': 18, 'scale': 2},
                {'name': 'ID', 'type': 'NUMBER', 'precision': 19, 'scale': 0}]
        out = enc('oracle', cols, {
            'CREATED': datetime.datetime(1970, 1, 2), 'TS': datetime.datetime(1970, 1, 1, 0, 0, 0, 7),
            'AMOUNT': D('12.5'), 'ID': D('42')})
        self.assertEqual(out, {'CREATED': '86400000', 'TS': '7', 'AMOUNT': '12.50', 'ID': '42'})


# The expected strings below marked "Java" were produced by
# tests/reference/DebeziumRef.java: the Debezium 3.0.6 code paths (with pgjdbc
# 42.6.1 and Jackson 2.16.2, the versions Debezium 3.0.6 ships) run on a JVM.


class MySQLZeroDateTest(unittest.TestCase):
    """Jira story F074: MySQL zero dates are written as Debezium writes them.

    Source: Debezium's MySQL documentation ("zero-values ... are represented as
    null values when the column allows nulls, or as the epoch when it does
    not") and RowDeserializers.deserializeDate/deserializeDatetime (a zero year,
    month or day is NULL). A TIMESTAMP is stored as seconds, so a zero
    TIMESTAMP is the epoch either way.
    """
    COLS = [
        {'name': 'd_null', 'type': 'date', 'nullable': 'YES'},
        {'name': 'd_nn', 'type': 'date', 'nullable': 'NO'},
        {'name': 'dt_null', 'type': 'datetime', 'precision': 0, 'nullable': 'YES'},
        {'name': 'dt6_nn', 'type': 'datetime', 'precision': 6, 'nullable': 'NO'},
        {'name': 'ts_null', 'type': 'timestamp', 'precision': 0, 'nullable': 'YES'},
        {'name': 'ts3_nn', 'type': 'timestamp', 'precision': 3, 'nullable': 'NO'},
    ]

    def test_zero_values(self):
        # pymysql returns a zero date as the server's text.
        out = enc('mysql', self.COLS, {
            'd_null': '0000-00-00', 'd_nn': '0000-00-00',
            'dt_null': '0000-00-00 00:00:00', 'dt6_nn': '0000-00-00 00:00:00.000000',
            'ts_null': '0000-00-00 00:00:00', 'ts3_nn': '0000-00-00 00:00:00.000'})
        self.assertEqual(out, {'d_null': '', 'd_nn': '0', 'dt_null': '', 'dt6_nn': '0',
                               'ts_null': '1970-01-01T00:00:00Z',
                               'ts3_nn': '1970-01-01T00:00:00.000Z'})

    def test_a_zero_part_is_a_zero_date(self):
        out = enc('mysql', self.COLS, {'d_null': '2024-00-15', 'd_nn': '2024-05-00',
                                       'dt_null': '0000-03-01 10:00:00'})
        self.assertEqual(out, {'d_null': '', 'd_nn': '0', 'dt_null': ''})

    def test_bytes_and_real_dates_are_unchanged(self):
        out = enc('mysql', self.COLS, {'d_null': b'0000-00-00',
                                       'd_nn': datetime.date(2024, 2, 29),
                                       'dt6_nn': datetime.datetime(2024, 3, 5, 6, 7, 8, 123456)})
        self.assertEqual(out, {'d_null': '', 'd_nn': '19782', 'dt6_nn': '1709618828123456'})

    def test_no_nullability_is_read_as_nullable(self):
        out = enc('mysql', [{'name': 'd', 'type': 'date'}], {'d': '0000-00-00'})
        self.assertEqual(out, {'d': ''})

    def test_an_invalid_date_fails_with_its_value(self):
        # ALLOW_INVALID_DATES: Debezium's default failure mode stops on it too.
        with self.assertRaises(ValueError) as caught:
            enc('mysql', self.COLS, {'d_nn': '2024-02-31'})
        self.assertIn('2024-02-31', str(caught.exception))
        with self.assertRaises(ValueError):
            enc('mysql', self.COLS, {'dt_null': 'garbage'})

    def test_the_catalogue_query_reads_nullability(self):
        import bulk_loader
        from tests.test_source_table import _adapter
        a = _adapter('mysql', [('d', 'date', None, None, 'date', 'NO')])
        cols = a.column_types('shop.t')
        self.assertIn('IS_NULLABLE', a.conn.cur.calls[-1][0])
        self.assertEqual(cols[0]['nullable'], 'NO')
        self.assertEqual(enc('mysql', cols, {'d': '0000-00-00'}), {'d': '0'})


class MySQLUnsignedBigintTest(unittest.TestCase):
    """Review finding M9: BIGINT UNSIGNED is its exact value, as Debezium sends
    it with bigint.unsigned.handling.mode=precise. It used to be the 64 bits
    as a signed long (Jira story F073), so the largest value was cached as -1."""
    COLS = [{'name': 'u', 'type': 'bigint', 'column_type': 'bigint unsigned'},
            {'name': 'z', 'type': 'bigint', 'column_type': 'bigint(20) unsigned zerofill'},
            {'name': 's', 'type': 'bigint', 'column_type': 'bigint'}]
    def test_values(self):
        for value in (0, 12345, 9223372036854775807, 9223372036854775808, 18446744073709551615):
            self.assertEqual(enc('mysql', self.COLS, {'u': value, 'z': value})['u'], str(value))
            self.assertEqual(enc('mysql', self.COLS, {'z': value})['z'], str(value))
        self.assertEqual(enc('mysql', self.COLS, {'s': -5}), {'s': '-5'})
    def test_the_key_is_the_one_cdc_uses(self):
        import bulk_loader
        row = cv.row_encoder('mysql', [{'name': 'id', 'type': 'bigint',
                                        'column_type': 'bigint unsigned'}])(
            {'id': 18446744073709551615})
        self.assertEqual(bulk_loader.sink_pk(row, ['id']), '18446744073709551615')
    def test_debezium_is_told_to_send_the_exact_value(self):
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(here, 'debezium-server', 'application-mysql.properties')) as f:
            text = f.read()
        for line in ('bigint.unsigned.handling.mode=precise', 'format.value.decimal.format=NUMERIC',
                     'format.key.decimal.format=NUMERIC'):
            self.assertIn(line, text)
class PostgreSQLTypesTest(unittest.TestCase):
    """Jira story F075: bit, money, interval, hstore and temporal arrays."""

    def test_bit(self):
        cols = [{'name': 'b1', 'type': 'bit', 'length': 1},
                {'name': 'b3', 'type': 'bit', 'length': 3},
                {'name': 'b16', 'type': 'bit', 'length': 16},
                {'name': 'vb', 'type': 'bit varying', 'length': None},
                {'name': 'vb1', 'type': 'bit varying', 'length': 1}]
        # Java: convertBits / convertBit.
        out = enc('postgresql', cols, {'b1': '1', 'b3': '101', 'b16': '0000000100000010',
                                       'vb': '0' * 40 + '1', 'vb1': '0'})
        self.assertEqual(out, {'b1': 'true', 'b3': 'BQ==', 'b16': 'AgE=', 'vb': 'AQ==',
                               'vb1': 'false'})
        self.assertEqual(enc('postgresql', cols, {'b3': '000'}), {'b3': ''})
        self.assertEqual(cv.pg_bits('111111111'), '/wE=')
        self.assertEqual(cv.pg_bits('1000000000000000'), 'AIA=')

    def test_money(self):
        cols = [{'name': 'm', 'type': 'money'}]
        for text, want in [('$1,234.56', '1234.56'), ('-$1,234.56', '-1234.56'),
                           ('$0.00', '0.00'), ('-$0.01', '-0.01'), ('$5.10', '5.10'),
                           ('$92,233,720,368,547,758.07', '92233720368547758.07')]:
            self.assertEqual(enc('postgresql', cols, {'m': text}), {'m': want}, text)
        with self.assertRaises(ValueError):
            cv.pg_money('1.234,56 EUR')

    def test_interval(self):
        cols = [{'name': 'i', 'type': 'interval'}]
        for text, want in [
                ('1 year 2 mons 3 days 04:05:06.789', '37091106789000'),
                ('3 days 04:05:06.5', '273906500000'),
                ('1 mon', '2629800000000'),             # 365.25 / 12 days, not 30
                ('-1 days +02:03:00', '-79020000000'),
                ('-00:00:01.5', '-1500000'),
                ('00:00:00.000003', '3'),
                ('-3 years -2 mons', '-99932400000000'),
                ('2 mons -10 days 00:00:00.999999', '4395600999999'),
                ('-12:34:56.123456', '-45296123456'),
                ('1 year 00:00:00.3', '31557600300000'),
                ('178000000 years', '9223372036854775807'),   # Java's (long) saturates
                # PGInterval rounds the fraction to whole micros, then the
                # double arithmetic truncates: .006938 comes out as 6937.
                ('00:00:01.005440', '1005440'),
                ('00:00:01.006938', '1006937'),
                ('-00:00:01.005440', '-1005440'),
                ('00:00:00', '0')]:
            self.assertEqual(enc('postgresql', cols, {'i': text}), {'i': want}, text)

    def test_hstore(self):
        cols = [{'name': 'h', 'type': 'USER-DEFINED', 'udt': 'hstore'}]
        # Java: HashMap order, Jackson escaping.
        for text, want in [
                ('"b"=>"2", "a"=>"1"', '{"a":"1","b":"2"}'),
                ('"a"=>NULL', '{"a":null}'),
                ('', '{}'),
                ('"key10"=>"x", "key2"=>"y", "key1"=>"z", "Key"=>"q"',
                 '{"key1":"z","key2":"y","key10":"x","Key":"q"}'),
                ('"zz"=>"1", "\\\\"=>"2", "€"=>"3", "😀"=>"4"',
                 '{"zz":"1","😀":"4","\\\\":"2","€":"3"}'),
                ('"q\\"uote"=>"back\\\\slash", "ctl"=>"\x01\x1f\x7f", "tab"=>"\t\n"',
                 '{"tab":"\\t\\n","q\\"uote":"back\\\\slash","ctl":"\\u0001\\u001F\x7f"}')]:
            self.assertEqual(enc('postgresql', cols, {'h': text}), {'h': want}, text)

    def test_hstore_order_after_the_map_grows(self):
        # 13 and 40 entries: HashMap resizes past 12 and 24 entries. Java.
        text = ', '.join('"k%d"=>"v%d"' % (i, i) for i in range(13))
        self.assertEqual(cv.pg_hstore(text),
                         '{"k0":"v0","k1":"v1","k2":"v2","k3":"v3","k4":"v4","k5":"v5",'
                         '"k11":"v11","k6":"v6","k10":"v10","k7":"v7","k8":"v8","k12":"v12",'
                         '"k9":"v9"}')
        text = ', '.join('"field_%d"=>"%d"' % (i, i * 7) for i in range(40))
        self.assertTrue(cv.pg_hstore(text).startswith(
            '{"field_0":"0","field_20":"140","field_3":"21","field_21":"147","field_4":"28"'))

    def test_temporal_arrays(self):
        cols = [{'name': 'ds', 'type': 'ARRAY', 'udt': '_date'},
                {'name': 'ts', 'type': 'ARRAY', 'udt': '_timestamp', 'typmod': -1},
                {'name': 'ts3', 'type': 'ARRAY', 'udt': '_timestamp', 'typmod': 3},
                {'name': 'tz', 'type': 'ARRAY', 'udt': '_timestamptz', 'typmod': -1},
                {'name': 'tm', 'type': 'ARRAY', 'udt': '_time', 'typmod': -1},
                {'name': 'ttz', 'type': 'ARRAY', 'udt': '_timetz'},
                {'name': 'ints', 'type': 'ARRAY', 'udt': '_int4'}]
        out = enc('postgresql', cols, {
            'ds': [datetime.date(2024, 2, 29), None, datetime.date(1969, 12, 31)],
            'ts': [datetime.datetime(2024, 3, 5, 6, 7, 8, 123456)],
            'ts3': [datetime.datetime(2024, 3, 5, 6, 7, 8, 120000)],
            'tz': [datetime.datetime(2024, 3, 5, 8, 7, 8, 120000,
                                     tzinfo=datetime.timezone(datetime.timedelta(hours=2)))],
            'tm': [datetime.time(1, 2, 3, 4)],
            'ttz': [datetime.time(13, 14, 15, 500000, tzinfo=UTC)],
            'ints': [1, None, 3]})
        self.assertEqual(out, {'ds': '[19782,null,-1]', 'ts': '[1709618828123456]',
                               'ts3': '[1709618828120]',
                               'tz': '["2024-03-05T06:07:08.120000Z"]',
                               'tm': '[3723000004]', 'ttz': '["13:14:15.5Z"]',
                               'ints': '[1,null,3]'})
        self.assertEqual(enc('postgresql', cols, {'ds': []}), {'ds': '[]'})

    def test_arrays_debezium_leaves_out_are_left_out(self):
        # PostgresValueConverter.schemaBuilder returns null for these: the
        # column is not in the change event at all.
        cols = [{'name': 'ms', 'type': 'ARRAY', 'udt': '_money'},
                {'name': 'iv', 'type': 'ARRAY', 'udt': '_interval'},
                {'name': 'vb', 'type': 'ARRAY', 'udt': '_varbit'},
                {'name': 'id', 'type': 'integer'}]
        self.assertEqual(enc('postgresql', cols, {'ms': ['$1.00'], 'iv': None, 'vb': '{1}',
                                                  'id': 5}), {'id': '5'})

    def test_the_catalogue_query_reads_what_the_formats_need(self):
        from tests.test_source_table import _adapter
        a = _adapter('postgresql', [('h', 'USER-DEFINED', None, None, 'hstore', None, 'YES', -1),
                                    ('b', 'bit', None, None, 'bit', 1, 'NO', 1)])
        cols = a.column_types('public.t')
        sql = a.conn.cur.calls[-1][0]
        for part in ('udt_name', 'character_maximum_length', 'atttypmod'):
            self.assertIn(part, sql)
        self.assertEqual(enc('postgresql', cols, {'h': '"a"=>"1"', 'b': '1'}),
                         {'h': '{"a":"1"}', 'b': 'true'})


class UnverifiedColumnsTest(unittest.TestCase):
    def test_types_without_a_checked_format_are_named(self):
        cols = [{'name': 'id', 'type': 'integer'}, {'name': 'g', 'type': 'USER-DEFINED',
                                                    'udt': 'geometry'},
                {'name': 'p', 'type': 'point', 'udt': 'point'},
                {'name': 'm', 'type': 'money'}, {'name': 'h', 'type': 'USER-DEFINED',
                                                 'udt': 'hstore'},
                {'name': 'r', 'type': 'int4range', 'udt': 'int4range'},
                {'name': 'a', 'type': 'ARRAY', 'udt': '_int4'},
                {'name': 'ap', 'type': 'ARRAY', 'udt': '_point'}]
        self.assertEqual(cv.unverified_columns('postgresql', cols),
                         ['g (geometry)', 'p (point)', 'ap (_point)'])
        self.assertEqual(cv.unverified_columns('mysql', cols), [])


class ZonedTimestampWidthTest(unittest.TestCase):
    """Jira story F076. PostgreSQL timestamptz: PostgresValueConverter.convertTimestampWithZone
    formats with a fixed width, the column's scale, which pgjdbc reports as the
    precision (6 unless declared; TypeInfoCache.getScale), so the loaded value
    keeps its width on the first update. SQL Server datetimeoffset is
    formatted from an OffsetDateTime at the value's own offset
    (SqlServerValueConverters), with ISO_OFFSET_DATE_TIME: no fixed width, the
    offset kept. Values Java."""

    def test_postgresql_timestamptz(self):
        def tz(p, v):
            return enc('postgresql', [{'name': 'c', 'type': 'timestamp with time zone',
                                       'precision': p}], {'c': v})['c']
        plus2 = datetime.timezone(datetime.timedelta(hours=2))
        self.assertEqual(tz(6, datetime.datetime(2024, 3, 5, 8, 7, 8, 120000, tzinfo=plus2)),
                         '2024-03-05T06:07:08.120000Z')
        self.assertEqual(tz(6, datetime.datetime(2024, 3, 5, 6, 7, 8, tzinfo=UTC)),
                         '2024-03-05T06:07:08.000000Z')
        self.assertEqual(tz(None, datetime.datetime(2024, 3, 5, 6, 7, 8, tzinfo=UTC)),
                         '2024-03-05T06:07:08.000000Z')
        self.assertEqual(tz(3, datetime.datetime(2024, 3, 5, 6, 7, 8, 123456, tzinfo=UTC)),
                         '2024-03-05T06:07:08.123Z')
        self.assertEqual(tz(1, datetime.datetime(2024, 3, 5, 6, 7, 8, 1, tzinfo=UTC)),
                         '2024-03-05T06:07:08.0Z')
        self.assertEqual(tz(0, datetime.datetime(2024, 3, 5, 6, 7, 8, 100000, tzinfo=UTC)),
                         '2024-03-05T06:07:08.1Z')
        self.assertEqual(tz(6, datetime.datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)),
                         '1969-12-31T23:59:59.500000Z')

    def test_sqlserver_datetimeoffset_keeps_its_offset(self):
        def off(v):
            return enc('sqlserver', [{'name': 'c', 'type': 'datetimeoffset',
                                      'precision': 7}], {'c': v})['c']
        tzinfo = datetime.timezone

        def at(h, m=0):
            return tzinfo(datetime.timedelta(hours=h, minutes=m))
        self.assertEqual(off(datetime.datetime(2024, 3, 5, 11, 37, 8, 120000, tzinfo=at(5, 30))),
                         '2024-03-05T11:37:08.12+05:30')
        self.assertEqual(off(datetime.datetime(2024, 1, 1, 0, 0, 0, 100000, tzinfo=at(-8))),
                         '2024-01-01T00:00:00.1-08:00')
        self.assertEqual(off(datetime.datetime(2024, 1, 1, 23, 59, 59, 999999, tzinfo=at(14))),
                         '2024-01-01T23:59:59.999999+14:00')
        self.assertEqual(off(datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=at(0, -30))),
                         '2024-06-01T12:00:00-00:30')
        self.assertEqual(off(datetime.datetime(2024, 1, 1)), '2024-01-01T00:00:00Z')


class NumberFormatTest(unittest.TestCase):
    def test_java_double(self):
        for x, want in [(1.5, '1.5'), (100.0, '100.0'), (0.001, '0.001'),
                        (0.000123, '1.23E-4'), (1e7, '1.0E7'), (9999999.0, '9999999.0'),
                        (-2.5e-10, '-2.5E-10'), (0.0, '0.0')]:
            self.assertEqual(cv.java_double(x), want, x)

    def test_java_float(self):
        # MySQL FLOAT 1.1 is a 32-bit float; Java prints its shortest form.
        self.assertEqual(cv.java_float(1.100000023841858), '1.1')
        self.assertEqual(cv.java_float(0.000123), '1.23E-4')

    def test_generic_fallback(self):
        out = cv.generic_encoder({'a': True, 'b': b'\x01', 'c': D('1.50'), 'd': None,
                                  'e': {'y': 1, 'x': [2]}})
        self.assertEqual(out, {'a': 'true', 'b': 'AQ==', 'c': '1.50', 'd': '', 'e': '{"x":[2],"y":1}'})


class LoadRangeUsesColumnTypesTest(unittest.TestCase):
    """load_range writes the CDC format end to end, from the adapter's catalogue."""

    def test_loaded_row_matches_cdc(self):
        import bulk_loader
        from tests import test_denorm as fakes

        class TypedAdapter(fakes.FakeAdapter):
            def column_types(self, table):
                return MySQLTest.COLS + [{'name': 'id', 'type': 'int'}]

        store = fakes.FakeStore()
        rows = [{'id': 1, 'created': datetime.datetime(2025, 1, 1),
                 'changed_at': datetime.datetime(2026, 11, 11, 16, 34), 'b1': b'\x00',
                 'name': 'x', 'qty': None}]
        saved = bulk_loader.ADAPTERS, bulk_loader.target_client
        bulk_loader.ADAPTERS = {'mysql': lambda cfg: TypedAdapter(rows)}
        bulk_loader.target_client = lambda cfg: fakes.FakeValkey(store)
        try:
            bulk_loader.load_range(('mysql', {}, {}, 'e2e_items', 'id', 'e2e_items:', 1, 10,
                                    None, 'e2e_items', 0, ''))
        finally:
            bulk_loader.ADAPTERS, bulk_loader.target_client = saved
        self.assertEqual(store.hashes['e2e_items:1'], {
            'id': '1', 'created': '1735689600000', 'changed_at': '2026-11-11T16:34:00Z',
            'b1': 'false', 'name': 'x', 'qty': ''})


if __name__ == '__main__':
    unittest.main()
