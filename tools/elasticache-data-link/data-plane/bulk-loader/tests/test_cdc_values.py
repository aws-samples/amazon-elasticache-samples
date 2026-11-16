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
        self.assertEqual(out['tz'], '2024-03-05T06:07:08.12Z')
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
        self.assertEqual(out['off'], '2024-01-01T00:00:00Z')
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
