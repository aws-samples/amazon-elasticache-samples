"""Hardening of the older parts of app.py (the Medium/Low review round).

  Jira story F162  the background health monitor (broker port probes, false CloudWatch
        metrics) is gone and nothing starts it
  Jira story F166  app.py imports without ecdl_link, so the "links unavailable" routes
        can actually be served
  Jira story F153  source passwords: never stored, read from a Secrets Manager secret,
        no fallback to a stored copy
  Jira story F154  the Randomizer API is off unless ECDL_ENABLE_RANDOMIZER is set, and a
        run stops after max_rows attempts or max_seconds
  Jira story F158  restore accepts only an intact ECDL database, within a size limit,
        through a private temporary file, and then migrates it
  Jira story F159  request bodies are limited to 1 MiB; /api/links/parse refuses YAML
        aliases
  Jira stories F155/F161  writes are accepted from the page's own origin (the UI forwards
        /api/* through its nginx) and from listed origins, not from any page
        on the same IP or localhost

Run with:  python3 tests/test_app_hardening.py     (from the backend/ directory)
"""
import io
import json
import os
import sqlite3
import subprocess  # nosec B404 - runs this interpreter on a fixed snippet
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

_TMP = tempfile.mkdtemp(prefix='ecdl-hardening-')
os.environ['DATABASE_PATH'] = os.path.join(_TMP, 'app.db')
os.environ['ECDL_API_TOKEN'] = 'test-token-for-hardening'  # nosec B105 - fake test credential
os.environ.pop('ECDL_AUTH_DISABLED', None)
os.environ.pop('ECDL_ENABLE_RANDOMIZER', None)
os.environ['DEMO_HISTORY_ENABLED'] = 'false'   # no background sampler in a test

import app as app_module  # noqa: E402

AUTH = {'Authorization': 'Bearer test-token-for-hardening'}

with open(os.path.join(_BACKEND, 'app.py')) as _fh:
    APP_SOURCE = _fh.read()
# The code without comment lines, which may describe what was removed.
APP_CODE = '\n'.join(line for line in APP_SOURCE.splitlines()
                     if not line.lstrip().startswith('#'))


def _client():
    app_module.app.testing = True
    return app_module.app.test_client()


class HealthMonitorRemovedTest(unittest.TestCase):
    """Jira story F162. The thread probed every stored broker host:port every 60 s and
    published DebeziumReachable=0, because no Kafka Connect runs here."""

    def test_no_monitor_functions_remain(self):
        for name in ('start_health_monitor', '_pipeline_health_monitor',
                     '_check_debezium', '_check_kafka'):
            self.assertFalse(hasattr(app_module, name), name)

    def test_source_has_no_probe_or_metric_calls(self):
        for text in ('socket.create_connection', 'put_metric_data',
                     'localhost:8083', 'DebeziumReachable', 'start_health_monitor('):
            self.assertFalse(text in APP_CODE, text)

    def test_importing_the_app_starts_no_thread(self):
        # Run in a fresh interpreter: this process already imported app.py.
        code = (
            'import os, sys, threading\n'
            'sys.path.insert(0, {backend!r})\n'
            'os.environ["DATABASE_PATH"] = {db!r}\n'
            'os.environ["DEMO_HISTORY_ENABLED"] = "false"\n'
            'before = threading.active_count()\n'
            'import app\n'
            'app.init_db()\n'
            'print(threading.active_count() - before)\n'
        ).format(backend=_BACKEND, db=os.path.join(_TMP, 'threads.db'))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, check=True, timeout=60)  # nosec B603
        self.assertEqual(out.stdout.strip().splitlines()[-1], '0', out.stderr)


# A fresh interpreter in which `import ecdl_link` fails, as it does in a
# container that mounts only backend/.
_NO_ECDL_LINK = '''
import json, os, sys
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == 'ecdl_link' or name.startswith('ecdl_link.'):
            raise ImportError('blocked for the test: ' + name)
        return None
sys.meta_path.insert(0, _Block())
sys.path.insert(0, {backend!r})
os.environ['DATABASE_PATH'] = {db!r}
os.environ['DEMO_HISTORY_ENABLED'] = 'false'
import app
app.app.testing = True
res = app.app.test_client().get('/api/links/schema')
print(json.dumps({{'status': res.status_code, 'body': res.get_json()}}))
'''


class LinksFallbackTest(unittest.TestCase):
    """Jira story F166. app.py imported ecdl_link at module level, so without it the app
    did not start and the 503 explaining why was never served."""

    def _run(self):
        code = _NO_ECDL_LINK.format(backend=_BACKEND,
                                    db=os.path.join(_TMP, 'fallback.db'))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, timeout=60)  # nosec B603
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_app_starts_and_explains_without_ecdl_link(self):
        got = self._run()
        self.assertEqual(got['status'], 503)
        self.assertIn('not available', got['body']['error'])
        self.assertIn('ecdl_link', got['body']['reason'])
        self.assertIn('ecdl_link', got['body']['hint'])

    def test_no_module_level_ecdl_link_import(self):
        for line in APP_SOURCE.splitlines():
            if line.startswith(('from ecdl_link', 'import ecdl_link')):
                self.fail('module-level import: ' + line)

    def test_normal_start_serves_the_real_links_api(self):
        res = _client().get('/api/links/schema')
        self.assertEqual(res.status_code, 200)


ARN = 'arn:aws:secretsmanager:eu-west-1:111122223333:secret:shop-db-AbCdEf'
SOURCE = {'name': 'shop', 'engine': 'postgres', 'host': 'db.example',
          'database_name': 'shop', 'username': 'row-user'}


def _db():
    return sqlite3.connect(app_module.DATABASE_PATH)


class _FakeSecrets:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.asked = value, error, []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's name
        self.asked.append(SecretId)
        if self.error:
            raise self.error
        return {'SecretString': self.value}


class SourcePasswordTest(unittest.TestCase):
    """Jira story F153."""

    def setUp(self):
        app_module.init_db()
        conn = _db()
        conn.execute('DELETE FROM sources')
        conn.commit()
        conn.close()

    def _rows(self):
        conn = _db()
        rows = conn.execute('SELECT name, password, password_secret_arn FROM sources').fetchall()
        conn.close()
        return rows

    def _create(self, **over):
        return _client().post('/api/sources', headers=AUTH, json=dict(SOURCE, **over))

    def test_create_with_a_password_is_refused_and_stores_nothing(self):
        res = self._create(password='s3cret')  # nosec B106 - fake test value
        self.assertEqual(res.status_code, 400)
        self.assertIn('Secrets Manager', res.get_json()['error'])
        self.assertEqual(self._rows(), [])

    def test_create_with_an_empty_password_and_an_arn_stores_only_the_arn(self):
        res = self._create(password='', password_secret_arn=ARN)  # nosec B106 - fake test value
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self._rows(), [('shop', None, ARN)])
        listed = _client().get('/api/sources').get_json()
        self.assertTrue(listed[0]['has_password'])
        self.assertEqual(listed[0]['password_secret_arn'], ARN)
        self.assertNotIn('password', listed[0])

    def test_create_without_a_secret_has_no_password(self):
        self.assertEqual(self._create().status_code, 201)
        self.assertFalse(_client().get('/api/sources').get_json()[0]['has_password'])

    def test_a_malformed_arn_is_refused(self):
        for bad in ('shop-db', 'arn:aws:s3:::bucket', ARN + "' OR 1=1",
                    'arn:aws:secretsmanager:eu-west-1:1234:secret:x', 42):
            res = self._create(password_secret_arn=bad)
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(self._rows(), [])

    def test_update_refuses_a_password_and_keeps_clears_or_sets_the_arn(self):
        self._create(password_secret_arn=ARN)
        sid = _client().get('/api/sources').get_json()[0]['id']
        put = lambda **b: _client().put('/api/sources/{}'.format(sid), headers=AUTH,  # noqa: E731
                                        json=dict(SOURCE, **b))
        self.assertEqual(put(password='new').status_code, 400)  # nosec B106 - fake test value
        self.assertEqual(self._rows(), [('shop', None, ARN)])
        self.assertEqual(put().status_code, 200)                 # key absent: kept
        self.assertEqual(self._rows(), [('shop', None, ARN)])
        self.assertEqual(put(password_secret_arn='bad').status_code, 400)  # nosec B106 - fake test value
        self.assertEqual(self._rows(), [('shop', None, ARN)])
        other = ARN.replace('shop-db', 'other')
        self.assertEqual(put(password_secret_arn=other).status_code, 200)
        self.assertEqual(self._rows(), [('shop', None, other)])
        self.assertEqual(put(password_secret_arn='').status_code, 200)  # nosec B106 - fake test value
        self.assertEqual(self._rows(), [('shop', None, '')])

    def test_init_db_blanks_passwords_an_older_version_stored(self):
        conn = _db()
        conn.execute("INSERT INTO sources (name, engine, host, database_name, password, "
                     "password_secret_arn, created) VALUES "
                     "('old', 'postgres', 'h', 'd', 'plain', '', '2026-01-01'), "
                     "('empty', 'postgres', 'h', 'd', '', '', '2026-01-01')")
        conn.commit()
        conn.close()
        app_module.init_db()
        self.assertEqual(sorted(self._rows()), [('empty', '', ''), ('old', None, '')])

    def test_credentials_come_from_the_secret_in_the_arns_region(self):
        fake = _FakeSecrets(json.dumps({'username': 'secret-user', 'password': 'pw1'}))  # nosec B105 - fake test value
        with mock.patch.object(app_module.boto3, 'client', return_value=fake) as client:
            got = app_module.source_credentials(dict(SOURCE, password_secret_arn=ARN))
        self.assertEqual(got, ('secret-user', 'pw1'))
        self.assertEqual(client.call_args, mock.call('secretsmanager', region_name='eu-west-1'))
        self.assertEqual(fake.asked, [ARN])

    def test_a_plain_text_secret_uses_the_sources_username(self):
        fake = _FakeSecrets('pw2')
        with mock.patch.object(app_module.boto3, 'client', return_value=fake):
            got = app_module.source_credentials(dict(SOURCE, password_secret_arn=ARN))
        self.assertEqual(got, ('row-user', 'pw2'))

    def test_no_secret_fails_closed_even_with_a_stored_password(self):
        src = dict(SOURCE, password='plain', password_secret_arn='')  # nosec B106 - fake test value
        with mock.patch.object(app_module.boto3, 'client') as client:
            with self.assertRaises(app_module.SourceCredentialError):
                app_module.source_credentials(src)
            with self.assertRaises(app_module.SourceCredentialError):
                app_module._connect_source_db(dict(src, port=5432))
        client.assert_not_called()

    def test_an_unreadable_secret_is_an_error_with_the_reason(self):
        fake = _FakeSecrets(error=Exception('AccessDeniedException: not authorized'))
        with mock.patch.object(app_module.boto3, 'client', return_value=fake):
            with self.assertRaises(app_module.SourceCredentialError) as caught:
                app_module.source_credentials(dict(SOURCE, password_secret_arn=ARN))
        self.assertIn('not permitted', str(caught.exception))

    def test_scan_tables_without_a_secret_is_400_before_connecting(self):
        self._create()
        sid = _client().get('/api/sources').get_json()[0]['id']
        res = _client().get('/api/sources/{}/scan-tables'.format(sid))
        self.assertEqual(res.status_code, 400)
        self.assertIn('no password secret', res.get_json()['error'])


RANDOMIZER_ROUTES = (
    ('POST', '/api/randomizer/start'),
    ('POST', '/api/randomizer/stop'),
    ('GET', '/api/randomizer/status?source_id=1'),
    ('GET', '/api/sources/1/tables'),
    ('GET', '/api/sources/1/columns?table=orders'),
)


def _start_body(**over):
    body = {'source_id': 1, 'table': 'orders', 'schema': 'public',
            'columns': [{'name': 'note', 'type': 'text'}],
            'batch_size': 1, 'interval': 1}
    body.update(over)
    return body


class _FakeCursor:
    def __init__(self, fail_inserts=False):
        self.fail_inserts = fail_inserts
        self.inserts = 0

    def execute(self, sql, params=None):
        if sql.startswith('INSERT'):
            if self.fail_inserts:
                raise RuntimeError('rejected')
            self.inserts += 1

    def fetchall(self):
        return [('note',)]

    def close(self):
        pass


class _FakeDb:
    autocommit = True

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass

    def close(self):
        pass


class RandomizerSwitchTest(unittest.TestCase):
    """Jira story F154. Off by default; bounded when on."""

    def setUp(self):
        os.environ.pop('ECDL_ENABLE_RANDOMIZER', None)
        self.addCleanup(os.environ.pop, 'ECDL_ENABLE_RANDOMIZER', None)

    def _call(self, method, path, body=None):
        c = _client()
        if method == 'POST':
            return c.post(path, headers=AUTH, json=body or _start_body())
        return c.get(path)

    def test_every_randomizer_route_is_404_when_off(self):
        before = dict(app_module._randomizer_threads)
        for method, path in RANDOMIZER_ROUTES:
            res = self._call(method, path)
            self.assertEqual(res.status_code, 404, path)
            self.assertIn('ECDL_ENABLE_RANDOMIZER', res.get_json()['hint'])
        self.assertEqual(app_module._randomizer_threads, before)

    def test_flag_values(self):
        for value, on in (('true', True), ('1', True), ('YES', True), ('on', True),
                          ('false', False), ('0', False), ('', False), ('maybe', False)):
            os.environ['ECDL_ENABLE_RANDOMIZER'] = value
            self.assertEqual(app_module.randomizer_enabled(), on, value)

    def test_when_on_the_routes_answer(self):
        os.environ['ECDL_ENABLE_RANDOMIZER'] = 'true'
        res = self._call('GET', '/api/randomizer/status?source_id=999')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['status'], 'idle')

    def test_limits_are_validated(self):
        os.environ['ECDL_ENABLE_RANDOMIZER'] = 'true'
        for over in ({'max_rows': 100001}, {'max_rows': -1}, {'max_seconds': 3601},
                     {'max_seconds': -5}, {'source_id': 'x'}, {'source_id': True},
                     {'source_id': None}):
            res = self._call('POST', '/api/randomizer/start', _start_body(**over))
            self.assertEqual(res.status_code, 400, over)

    def test_defaults_are_applied(self):
        req = app_module._randomizer_request(_start_body())
        self.assertEqual(req['max_rows'], app_module.RANDOMIZER_DEFAULT_MAX_ROWS)
        self.assertEqual(req['max_seconds'], app_module.RANDOMIZER_DEFAULT_MAX_SECONDS)
        req = app_module._randomizer_request(_start_body(max_rows=5, max_seconds=7))
        self.assertEqual((req['max_rows'], req['max_seconds']), (5, 7))


class RandomizerBoundsTest(unittest.TestCase):
    """Jira story F154. The worker loop, run synchronously against a fake source."""

    def setUp(self):
        app_module.init_db()
        conn = _db()
        conn.execute('DELETE FROM sources')
        cur = conn.execute(
            "INSERT INTO sources (name, engine, host, port, database_name, "
            "password_secret_arn, created) VALUES ('gen', 'postgres', 'h', 5432, 'd', ?, "
            "'2026-01-01')", (ARN,))
        self.sid = cur.lastrowid
        conn.commit()
        conn.close()

    def _run(self, cursor, stop=False, **limits):
        import threading
        evt = threading.Event()
        if stop:
            evt.set()
        app_module._randomizer_stop[self.sid] = evt
        status = {'status': 'running', 'records_written': 0, 'errors': 0, 'last_error': None}
        app_module._randomizer_threads[self.sid] = status
        with mock.patch.object(app_module, '_connect_source_db', return_value=_FakeDb(cursor)):
            # In a thread with a time limit, so a loop that does not stop is a
            # failure rather than a test run that never ends.
            worker = threading.Thread(target=app_module._randomizer_worker, args=(
                self.sid, 'orders', [{'name': 'note', 'type': 'text'}],
                10, 0, 'public'), kwargs=limits, daemon=True)
            worker.start()
            worker.join(20)
            if worker.is_alive():
                evt.set()
                worker.join(5)
                self.fail('the generator did not stop by itself: {}'.format(status))
        return status

    def test_stops_at_max_rows(self):
        cur = _FakeCursor()
        status = self._run(cur, max_rows=25, max_seconds=3600)
        self.assertEqual((status['status'], status['stop_reason']), ('stopped', 'max_rows'))
        self.assertEqual((status['records_written'], cur.inserts), (25, 25))

    def test_failed_inserts_count_towards_max_rows(self):
        status = self._run(_FakeCursor(fail_inserts=True), max_rows=7, max_seconds=3600)
        self.assertEqual(status['stop_reason'], 'max_rows')
        self.assertEqual((status['records_written'], status['errors']), (0, 7))

    def test_stops_at_max_seconds(self):
        clock = iter(range(0, 10 ** 6))
        with mock.patch.object(app_module._time, 'monotonic', side_effect=lambda: next(clock)):
            status = self._run(_FakeCursor(), max_rows=100000, max_seconds=5)
        self.assertEqual(status['stop_reason'], 'max_seconds')
        self.assertLess(status['records_written'], 10)

    def test_default_limits_apply_when_none_are_given(self):
        status = self._run(_FakeCursor())
        self.assertEqual(status['records_written'], app_module.RANDOMIZER_DEFAULT_MAX_ROWS)

    def test_a_stop_request_ends_it_with_nothing_written(self):
        status = self._run(_FakeCursor(), stop=True, max_rows=5, max_seconds=60)
        self.assertEqual((status['stop_reason'], status['records_written']), ('requested', 0))


def _sqlite_bytes(build):
    """The bytes of a SQLite file made by build(conn)."""
    fd, path = tempfile.mkstemp(suffix='.db', dir=_TMP)
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    build(conn)
    conn.commit()
    conn.close()
    with open(path, 'rb') as fh:
        data = fh.read()
    os.remove(path)
    return data


def _app_db(conn, extra_sql=()):
    """An ECDL database as an older version left it: no audit_log, no
    password_secret_arn, a stored source password."""
    conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                 "engine TEXT, host TEXT, port INTEGER, database_name TEXT, schema_name TEXT, "
                 "username TEXT, password TEXT, ssl_mode TEXT, status TEXT, created DATE, "
                 "notes TEXT)")
    for table in ('connectors', 'brokers', 'pipelines', 'deployments'):
        conn.execute('CREATE TABLE {} (id INTEGER PRIMARY KEY)'.format(table))
    conn.execute('CREATE TABLE targets (id INTEGER PRIMARY KEY, auth_token TEXT)')
    conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, flag INTEGER, '
                 'i_value INTEGER, s_value TEXT, f_value REAL)')
    conn.execute("INSERT INTO sources (name, engine, host, database_name, password, created) "
                 "VALUES ('restored', 'postgres', 'h', 'd', 'old-plain', '2026-01-01')")
    for sql in extra_sql:
        conn.execute(sql)


class RestoreTest(unittest.TestCase):
    """Jira story F158."""

    def setUp(self):
        app_module.init_db()
        self.db_dir = os.path.dirname(app_module.DATABASE_PATH)
        with open(app_module.DATABASE_PATH, 'rb') as fh:
            self.before = fh.read()

    def _restore(self, data, name='app.db'):
        return _client().post('/api/maintenance/restore', headers=AUTH,
                              data={'file': (io.BytesIO(data), name)},
                              content_type='multipart/form-data')

    def _current(self):
        with open(app_module.DATABASE_PATH, 'rb') as fh:
            return fh.read()

    def _leftovers(self):
        return [n for n in os.listdir(self.db_dir) if 'restore' in n]

    def _refused(self, data, text):
        res = self._restore(data)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn(text, res.get_json()['error'])
        self.assertEqual(self._current(), self.before, 'the database was replaced')
        self.assertEqual(self._leftovers(), [])

    def test_a_good_backup_is_restored_migrated_and_scrubbed(self):
        res = self._restore(_sqlite_bytes(_app_db))
        self.assertEqual(res.status_code, 200, res.data)
        conn = _db()
        row = conn.execute("SELECT name, password, password_secret_arn FROM sources").fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        # init_db ran: the new column and table exist, the stored password is gone.
        self.assertEqual(row, ('restored', None, ''))
        self.assertIn('audit_log', tables)
        self.assertEqual(self._leftovers(), [])

    def test_a_round_trip_through_backup_works(self):
        backup = _client().get('/api/maintenance/backup')
        self.assertEqual(backup.status_code, 200)
        self.assertEqual(self._restore(backup.data).status_code, 200)

    def test_not_sqlite_is_refused(self):
        self._refused(b'hello, this is not a database', 'not a SQLite database')
        self._refused(b'', 'not a SQLite database')

    def test_a_corrupt_database_is_refused(self):
        data = bytearray(_sqlite_bytes(_app_db))
        data[100:4096] = b'\xff' * (4096 - 100)      # trash the first page's b-tree
        self._refused(bytes(data), 'database')

    def test_a_database_with_a_damaged_table_fails_the_integrity_check(self):
        def many_rows(conn):
            _app_db(conn)
            for i in range(400):
                conn.execute("INSERT INTO settings (key, s_value) VALUES (?, ?)",
                             ('k{}'.format(i), 'v' * 200))
        data = bytearray(_sqlite_bytes(many_rows))
        page = 4096
        self.assertGreater(len(data), 12 * page)
        # The schema (page 1) is intact; one page of the settings b-tree is not.
        data[10 * page + 8:11 * page] = b'\x07' * (page - 8)
        self._refused(bytes(data), 'integrity check')

    def test_another_applications_database_is_refused(self):
        self._refused(_sqlite_bytes(lambda c: c.execute('CREATE TABLE things (x)')),
                      'not an ECDL control-plane database')

    def test_triggers_and_views_are_refused(self):
        trig = ("CREATE TRIGGER t AFTER INSERT ON settings BEGIN "
                "DELETE FROM sources; END",)
        self._refused(_sqlite_bytes(lambda c: _app_db(c, trig)), 'triggers or views')
        view = ("CREATE VIEW v AS SELECT 1",)
        self._refused(_sqlite_bytes(lambda c: _app_db(c, view)), 'triggers or views')

    def test_an_oversized_upload_is_413_and_changes_nothing(self):
        big = b'SQLite format 3\x00' + b'\0' * (app_module.RESTORE_MAX_BYTES + 1)
        res = self._restore(big)
        self.assertEqual(res.status_code, 413)
        self.assertEqual(self._current(), self.before)
        self.assertEqual(self._leftovers(), [])

    def test_an_upload_over_the_general_limit_is_allowed_for_restore(self):
        def padded(conn):
            _app_db(conn)
            conn.execute("INSERT INTO settings (key, s_value) VALUES ('pad', ?)",
                         ('x' * (2 * app_module.MAX_REQUEST_BYTES),))
        data = _sqlite_bytes(padded)
        self.assertGreater(len(data), app_module.MAX_REQUEST_BYTES)
        self.assertEqual(self._restore(data).status_code, 200)

    def test_the_temporary_file_is_private_and_unique(self):
        seen = []
        real = app_module.tempfile.mkstemp

        def spy(*a, **k):
            fd, path = real(*a, **k)
            seen.append(path)
            return fd, path
        with mock.patch.object(app_module.tempfile, 'mkstemp', side_effect=spy):
            self._restore(b'junk')
            self._restore(b'junk')
        self.assertEqual(len(set(seen)), 2)
        for path in seen:
            self.assertEqual(os.path.dirname(path), self.db_dir)
            self.assertNotEqual(path, app_module.DATABASE_PATH + '.restore_tmp')


class RequestLimitTest(unittest.TestCase):
    """Jira story F159."""

    def test_a_body_over_one_mib_is_413(self):
        text = 'name: x\n' + '#' * app_module.MAX_REQUEST_BYTES
        res = _client().post('/api/links/parse', headers=AUTH, json={'text': text})
        self.assertEqual(res.status_code, 413)
        self.assertIn('too large', res.get_json()['error'])

    def test_a_body_under_the_limit_is_served(self):
        res = _client().post('/api/links/parse', headers=AUTH, json={'text': 'name: x\n'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['parsed'])

    def test_yaml_aliases_are_refused_with_a_position(self):
        laughs = 'a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n'
        res = _client().post('/api/links/parse', headers=AUTH, json={'text': laughs})
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertFalse(body['parsed'])
        self.assertIn('aliases', body['error'])
        self.assertEqual(body['line'], 2)

    def test_a_merge_key_alias_is_refused(self):
        text = 'base: &b {name: x}\nlink:\n  <<: *b\n'
        body = _client().post('/api/links/parse', headers=AUTH, json={'text': text}).get_json()
        self.assertFalse(body['parsed'])

    def test_an_anchor_without_an_alias_still_parses(self):
        body = _client().post('/api/links/parse', headers=AUTH,
                              json={'text': 'name: &n x\n'}).get_json()
        self.assertTrue(body['parsed'])
        self.assertEqual(body['document'], {'name': 'x'})


class BrowserOriginTest(unittest.TestCase):
    """Jira stories F155/F161. Writes from a browser: same origin or a listed origin only."""

    SSM_HOSTS = 'localhost,127.0.0.1,[::1]'      # the template's ssm-mode list

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ('ECDL_ALLOWED_HOSTS', 'ECDL_ALLOWED_ORIGINS')}
        os.environ.pop('ECDL_ALLOWED_ORIGINS', None)
        app_module.init_db()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, host, origin=None):
        headers = dict(AUTH)
        if origin:
            headers['Origin'] = origin
        c = app_module.app.test_client()
        return c.post('/api/settings', base_url='http://' + host, headers=headers,
                      json={'key': 'origin-test', 's_value': 'x'})

    def test_ssm_tunnel_page_writes_through_its_own_origin(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = self.SSM_HOSTS
        self.assertEqual(self._write('localhost:3000', 'http://localhost:3000').status_code, 200)
        self.assertEqual(self._write('127.0.0.1:3000', 'http://127.0.0.1:3000').status_code, 200)
        # ecdl-ui --ui-port 3005 (Jira story F144): still the page's own origin.
        self.assertEqual(self._write('localhost:3005', 'http://localhost:3005').status_code, 200)

    def test_cidr_page_writes_through_its_own_origin(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = 'localhost,10.0.1.23'
        self.assertEqual(self._write('10.0.1.23:3000', 'http://10.0.1.23:3000').status_code, 200)

    def test_another_port_on_the_same_address_is_refused(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = 'localhost,127.0.0.1,10.0.1.23'
        for host, origin in (('localhost:5000', 'http://localhost:8080'),
                             ('127.0.0.1:5000', 'http://127.0.0.1:9999'),
                             ('10.0.1.23:5000', 'http://10.0.1.23:3000'),
                             ('localhost:3000', 'http://localhost:3002')):
            res = self._write(host, origin)
            self.assertEqual(res.status_code, 403, (host, origin))
            self.assertIn('ECDL_ALLOWED_ORIGINS', res.get_json()['hint'])

    def test_listed_origins_still_work(self):
        # docker compose: UI on :3001, API on :5006, the default list.
        os.environ['ECDL_ALLOWED_HOSTS'] = 'localhost'
        self.assertEqual(self._write('localhost:5006', 'http://localhost:3001').status_code, 200)
        os.environ['ECDL_ALLOWED_ORIGINS'] = 'http://localhost:8080'
        self.assertEqual(self._write('localhost:5000', 'http://localhost:8080').status_code, 200)

    def test_no_origin_header_is_not_a_browser_request(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = self.SSM_HOSTS
        self.assertEqual(self._write('localhost:5000').status_code, 200)

    def test_the_host_check_still_applies_behind_the_proxy(self):
        # nginx passes the browser's Host on; a rebinding name is refused.
        os.environ['ECDL_ALLOWED_HOSTS'] = self.SSM_HOSTS
        res = self._write('evil.example:3000', 'http://evil.example:3000')
        self.assertEqual(res.status_code, 403)


if __name__ == '__main__':
    unittest.main()
