"""SQL built from request input: names validated and quoted, values bound.

Covers the one HTTP endpoint whose request body names tables and columns
(/api/randomizer/start) and the deployment-status UPDATE that used to be
assembled with an f-string.

Run with:  python3 tests/test_sql_safety.py     (from the backend/ directory)
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

_TMP = tempfile.mkdtemp(prefix='ecdl-sqlsafety-')
os.environ['DATABASE_PATH'] = os.path.join(_TMP, 'app.db')
os.environ['ECDL_API_TOKEN'] = 'test-token-for-sql-safety'  # nosec B105 - fake test credential
os.environ.pop('ECDL_AUTH_DISABLED', None)
os.environ['DEMO_HISTORY_ENABLED'] = 'false'   # no background sampler in a test

import app as app_module  # noqa: E402
from ecdl_link.sqlident import UnsafeIdentifier  # noqa: E402

AUTH = {'Authorization': 'Bearer test-token-for-sql-safety'}

HOSTILE = ['orders`; DROP TABLE users; --', 'orders"; DROP TABLE users; --',
           "orders'; --", 'orders WHERE 1=1', 'a/*b*/', '']


class _Cursor:
    def __init__(self, columns):
        self.columns = columns
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return [(c,) for c in self.columns]


def _body(**over):
    body = {'source_id': 1, 'table': 'orders', 'schema': 'public',
            'columns': [{'name': 'id', 'type': 'int'}, {'name': 'amount', 'type': 'decimal'}],
            'batch_size': 1, 'interval': 1}
    body.update(over)
    return body


class RandomizerRequestTest(unittest.TestCase):

    def test_a_good_request_parses(self):
        req = app_module._randomizer_request(_body())
        self.assertEqual(req['table'], 'orders')

    def test_hostile_table_schema_and_column_names_are_refused(self):
        for bad in HOSTILE:
            with self.assertRaises(ValueError, msg=('table', bad)):
                app_module._randomizer_request(_body(table=bad))
            if bad:   # an empty schema means "public"
                with self.assertRaises(ValueError, msg=('schema', bad)):
                    app_module._randomizer_request(_body(schema=bad))
            with self.assertRaises(ValueError, msg=('column', bad)):
                app_module._randomizer_request(
                    _body(columns=[{'name': bad, 'type': 'int'}]))

    def test_bounds_on_batch_and_interval(self):
        for over in ({'batch_size': 0}, {'batch_size': 100000}, {'interval': -1},
                     {'interval': 99999}, {'columns': []}, {'columns': 'id'}):
            with self.assertRaises(ValueError, msg=over):
                app_module._randomizer_request(_body(**over))

    def test_the_endpoint_answers_400_and_starts_nothing(self):
        app_module.app.testing = True  # Flask's test mode for the test client
        client = app_module.app.test_client()
        # The Randomizer API is off unless switched on (Jira story F154).
        os.environ['ECDL_ENABLE_RANDOMIZER'] = 'true'
        self.addCleanup(os.environ.pop, 'ECDL_ENABLE_RANDOMIZER', None)
        before = dict(app_module._randomizer_threads)
        res = client.post('/api/randomizer/start', headers=AUTH,
                          data=json.dumps(_body(table='orders`; DROP TABLE users; --')),
                          content_type='application/json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('not a plain SQL identifier', json.loads(res.data)['error'])
        self.assertEqual(app_module._randomizer_threads, before)


class RandomizerInsertTest(unittest.TestCase):

    def test_mysql_insert_is_quoted_and_bound(self):
        cur = _Cursor(['id', 'amount', 'note'])
        sql = app_module._randomizer_insert_sql(
            cur, {'engine': 'mysql', 'database_name': 'shop'}, 'public', 'orders',
            ['id', 'amount'])
        self.assertEqual(sql, 'INSERT INTO `orders` (`id`, `amount`) VALUES (%s, %s)')
        # The column lookup itself binds the table name.
        self.assertEqual(cur.calls[0][1], ('shop', 'orders'))

    def test_postgres_insert_quotes_schema_and_table_separately(self):
        cur = _Cursor(['id'])
        sql = app_module._randomizer_insert_sql(
            cur, {'engine': 'postgres'}, 'sales', 'orders', ['id'])
        self.assertEqual(sql, 'INSERT INTO "sales"."orders" ("id") VALUES (%s)')

    def test_a_column_the_table_does_not_have_is_refused(self):
        cur = _Cursor(['id'])
        with self.assertRaises(ValueError):
            app_module._randomizer_insert_sql(
                cur, {'engine': 'mysql', 'database_name': 'shop'}, 'public', 'orders',
                ['id', 'secret'])

    def test_a_missing_table_is_refused(self):
        with self.assertRaises(ValueError):
            app_module._randomizer_insert_sql(
                _Cursor([]), {'engine': 'mysql', 'database_name': 'shop'},
                'public', 'nope', ['id'])

    def test_a_hostile_name_never_reaches_the_statement(self):
        cur = _Cursor(['id', 'x`) VALUES (1); DROP TABLE t; --'])
        with self.assertRaises(UnsafeIdentifier):
            app_module._randomizer_insert_sql(
                cur, {'engine': 'mysql', 'database_name': 'shop'}, 'public', 'orders',
                ['x`) VALUES (1); DROP TABLE t; --'])

    def test_unsupported_engine_refused(self):
        with self.assertRaises(ValueError):
            app_module._randomizer_insert_sql(
                _Cursor(['id']), {'engine': 'sqlite'}, 'public', 'orders', ['id'])


class DeploymentStatusTest(unittest.TestCase):
    """The fixed UPDATE must behave exactly like the f-string it replaced."""

    @classmethod
    def setUpClass(cls):
        app_module.init_db()
        app_module.app.testing = True  # Flask's test mode for the test client
        cls.client = app_module.app.test_client()

    def _deployment(self):
        conn = sqlite3.connect(os.environ['DATABASE_PATH'])
        cur = conn.execute("INSERT INTO deployments (pipeline_id, status, log) "
                           "VALUES (1, 'pending', 'first')")
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return new_id

    def _row(self, dep_id):
        conn = sqlite3.connect(os.environ['DATABASE_PATH'])
        row = conn.execute('SELECT status, completed, log FROM deployments WHERE id=?',
                           (dep_id,)).fetchone()
        conn.close()
        return row

    def _put(self, dep_id, body):
        return self.client.put('/api/deployments/{}/status'.format(dep_id),
                               headers=AUTH, data=json.dumps(body),
                               content_type='application/json')

    def test_status_only_keeps_completed_and_log(self):
        dep = self._deployment()
        self.assertEqual(self._put(dep, {'status': 'deploying'}).status_code, 200)
        self.assertEqual(self._row(dep), ('deploying', None, 'first'))

    def test_terminal_status_stamps_completed_and_log_is_replaced(self):
        dep = self._deployment()
        self.assertEqual(self._put(dep, {'status': 'running', 'log': 'second'}).status_code, 200)
        status, completed, log = self._row(dep)
        self.assertEqual((status, log), ('running', 'second'))
        self.assertIsNotNone(completed)

    def test_hostile_status_is_stored_as_data(self):
        dep = self._deployment()
        evil = "x', log='pwned' WHERE 1=1; --"
        self.assertEqual(self._put(dep, {'status': evil}).status_code, 200)
        self.assertEqual(self._row(dep), (evil, None, 'first'))


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} sql-safety tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)
