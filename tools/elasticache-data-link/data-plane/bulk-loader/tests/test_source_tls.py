"""Jira story F078: the loader verifies TLS to a MySQL or PostgreSQL source by default.

MySQL used to connect in plaintext and PostgreSQL with sslmode=prefer (any
certificate, or none). Now the certificate must chain to SOURCE_TLS_CA_FILE
(the image's RDS CA bundle plus system CAs) and name the host; SOURCE_TLS=false
is the explicit opt-out.
"""
import os
import re
import shutil
import socket
import ssl
import subprocess  # nosec B404 - openssl makes a throwaway CA for the handshake test
import sys
import tempfile
import threading
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.dirname(HERE)
sys.path.insert(0, LOADER)
import bulk_loader  # noqa: E402

BUNDLE = os.path.join(LOADER, 'rds-ca-bundle.pem')


class _Env(object):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ('SOURCE_TLS', 'SOURCE_TLS_CA_FILE')}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SettingTest(_Env, unittest.TestCase):
    def test_verified_by_default(self):
        self.assertEqual(bulk_loader.source_tls_from_env(),
                         {'tls': True, 'tls_ca_file': '/app/source-ca.pem'})

    def test_the_opt_out_is_explicit(self):
        for value in ('false', 'FALSE', 'off', '0', 'no', 'disabled'):
            os.environ['SOURCE_TLS'] = value
            self.assertEqual(bulk_loader.source_tls_from_env(),
                             {'tls': False, 'tls_ca_file': None}, value)

    def test_an_unknown_value_is_refused_not_guessed(self):
        for value in ('prefer', 'require', 'maybe'):
            os.environ['SOURCE_TLS'] = value
            with self.assertRaises(SystemExit) as caught:
                bulk_loader.source_tls_from_env()
            self.assertIn('SOURCE_TLS', str(caught.exception))

    def test_a_ca_file_can_be_named(self):
        os.environ['SOURCE_TLS_CA_FILE'] = BUNDLE
        self.assertEqual(bulk_loader.source_tls_from_env()['tls_ca_file'], BUNDLE)
        self.assertEqual(bulk_loader.source_tls({'tls': True, 'tls_ca_file': BUNDLE}), BUNDLE)

    def test_a_missing_ca_file_stops_instead_of_connecting_unverified(self):
        with self.assertRaises(SystemExit) as caught:
            bulk_loader.source_tls({'tls': True, 'tls_ca_file': '/nonexistent/ca.pem'})
        self.assertIn('/nonexistent/ca.pem', str(caught.exception))
        with self.assertRaises(SystemExit):
            bulk_loader.source_tls({})        # the image's default, absent here

    def test_off_means_no_ca(self):
        self.assertIsNone(bulk_loader.source_tls({'tls': False}))
        self.assertIn('not encrypted', bulk_loader.describe_source_tls({'tls': False}))


class _FakeDrivers(object):
    """pymysql and psycopg2 stand-ins that record connect()'s arguments."""

    def setUp(self):
        self.calls = []
        calls = self.calls
        self.saved_modules = {k: sys.modules.get(k) for k in (
            'pymysql', 'pymysql.cursors', 'psycopg2', 'psycopg2.extras',
            'psycopg2.extensions')}
        pymysql = types.ModuleType('pymysql')
        pymysql.cursors = types.ModuleType('pymysql.cursors')
        pymysql.cursors.DictCursor = object
        class Conn(object):
            pass
        pymysql.connect = lambda **kw: calls.append(('mysql', kw)) or Conn()
        psycopg2 = types.ModuleType('psycopg2')
        psycopg2.extras = types.ModuleType('psycopg2.extras')
        psycopg2.extensions = types.ModuleType('psycopg2.extensions')
        psycopg2.extras.RealDictCursor = object
        psycopg2.extras.register_default_json = lambda conn, loads: None
        psycopg2.extras.register_default_jsonb = lambda conn, loads: None
        self.registered = []
        psycopg2.extensions.new_type = lambda oids, name, fn: (oids, name, fn)
        psycopg2.extensions.register_type = lambda t, conn: self.registered.append(t)
        self.pg_conns = []

        def pg_connect(**kw):
            calls.append(('postgresql', kw))
            self.pg_conns.append(Conn())
            return self.pg_conns[-1]
        psycopg2.connect = pg_connect
        sys.modules.update({'pymysql': pymysql, 'pymysql.cursors': pymysql.cursors,
                            'psycopg2': psycopg2, 'psycopg2.extras': psycopg2.extras,
                            'psycopg2.extensions': psycopg2.extensions})

    def tearDown(self):
        for k, v in self.saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    CFG = {'host': 'db.cluster-x.us-east-1.rds.amazonaws.com', 'user': 'u',
           'password': 'p', 'database': 'shop'}  # nosec B105 - a fake driver's argument

    def connect(self, engine, **tls):
        bulk_loader.ADAPTERS[engine](dict(self.CFG, **tls)).connect()
        return self.calls[-1][1]


class ConnectTest(_FakeDrivers, unittest.TestCase):
    def test_mysql_verifies_by_default(self):
        kw = self.connect('mysql', tls=True, tls_ca_file=BUNDLE)
        ctx = kw['ssl']
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertGreaterEqual(len(ctx.get_ca_certs()), 100)

    def test_mysql_plaintext_only_when_turned_off(self):
        self.assertNotIn('ssl', self.connect('mysql', tls=False))

    def test_postgresql_verify_full(self):
        kw = self.connect('postgresql', tls=True, tls_ca_file=BUNDLE)
        self.assertEqual(kw['sslmode'], 'verify-full')
        self.assertEqual(kw['sslrootcert'], BUNDLE)

    def test_postgresql_off_is_disable_not_prefer(self):
        kw = self.connect('postgresql', tls=False)
        self.assertEqual(kw['sslmode'], 'disable')
        self.assertNotIn('sslrootcert', kw)

    def test_postgresql_reads_money_interval_and_ranges_as_text(self):
        # Jira story F075: CDC parses the server text of these.
        self.connect('postgresql', tls=False)
        oids = {oid for (oids, _name, _fn) in self.registered for oid in oids}
        self.assertTrue({790, 1186, 3904, 3926, 3906, 3908, 3910, 3912} <= oids)
        fn = self.registered[0][2]
        self.assertEqual(fn('$1,234.56', None), '$1,234.56')

    def test_a_missing_bundle_fails_before_connecting(self):
        with self.assertRaises(SystemExit):
            self.connect('mysql', tls=True, tls_ca_file='/nonexistent.pem')
        with self.assertRaises(SystemExit):
            self.connect('postgresql', tls=True, tls_ca_file='/nonexistent.pem')
        self.assertEqual(self.calls, [])


def _openssl(*args, cwd):
    subprocess.run(['openssl'] + list(args), cwd=cwd, check=True,  # nosec B603 B607
                   capture_output=True)


@unittest.skipUnless(shutil.which('openssl'), 'openssl not installed')
class HandshakeTest(unittest.TestCase):
    """mysql_ssl_context against a real TLS server with a throwaway CA."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='ecdl-tls-')
        d = cls.dir
        _openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key',
                 '-out', 'ca.pem', '-days', '2', '-subj', '/CN=ECDL test CA',
                 '-addext', 'basicConstraints=critical,CA:TRUE',
                 '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                 '-addext', 'subjectKeyIdentifier=hash', cwd=d)
        _openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'srv.key', '-out',
                 'srv.csr', '-subj', '/CN=localhost', cwd=d)
        with open(os.path.join(d, 'ext.cnf'), 'w') as handle:
            # Python 3.13+ verifies strictly: the key identifiers must be there.
            handle.write('subjectAltName=DNS:localhost\n'
                         'basicConstraints=CA:FALSE\n'
                         'keyUsage=critical,digitalSignature,keyEncipherment\n'
                         'extendedKeyUsage=serverAuth\n'
                         'subjectKeyIdentifier=hash\n'
                         'authorityKeyIdentifier=keyid,issuer\n')
        _openssl('x509', '-req', '-in', 'srv.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
                 '-CAcreateserial', '-out', 'srv.pem', '-days', '2', '-extfile', 'ext.cnf',
                 cwd=d)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(os.path.join(d, 'srv.pem'), os.path.join(d, 'srv.key'))
        cls.sock = socket.socket()
        cls.sock.bind(('127.0.0.1', 0))
        cls.sock.listen(8)
        cls.port = cls.sock.getsockname()[1]

        def serve():
            while True:
                try:
                    conn, _ = cls.sock.accept()
                except OSError:
                    return
                try:
                    with server.wrap_socket(conn, server_side=True) as tls:
                        tls.recv(1)
                except (ssl.SSLError, OSError):
                    pass
        threading.Thread(target=serve, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def handshake(self, ca, hostname):
        ctx = bulk_loader.mysql_ssl_context(ca)
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
                return tls.version()

    def test_the_right_ca_and_host_name_connect(self):
        self.assertIn(self.handshake(os.path.join(self.dir, 'ca.pem'), 'localhost'),
                      ('TLSv1.2', 'TLSv1.3'))

    def test_another_host_name_is_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(os.path.join(self.dir, 'ca.pem'), 'db.example.com')

    def test_a_certificate_from_another_ca_is_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(BUNDLE, 'localhost')


class ImageTest(unittest.TestCase):
    def test_the_bundle_is_amazon_rds_cas(self):
        with open(BUNDLE) as handle:
            text = handle.read()
        certs = re.findall(r'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----',
                           text, re.S)
        self.assertGreaterEqual(len(certs), 100)
        ctx = ssl.create_default_context(cafile=BUNDLE)
        cas = ctx.get_ca_certs()
        self.assertEqual(len(cas), len(certs))
        for ca in cas:
            subject = dict(x[0] for x in ca['subject'])
            self.assertEqual(subject.get('organizationalUnitName'), 'Amazon RDS', subject)

    def test_the_image_builds_the_default_ca_file(self):
        with open(os.path.join(LOADER, 'Dockerfile')) as handle:
            text = handle.read()
        self.assertIn('COPY rds-ca-bundle.pem', text)
        self.assertRegex(text, r'cat /app/rds-ca-bundle.pem /etc/ssl/certs/ca-certificates.crt'
                               r' > /app/source-ca.pem')
        self.assertEqual(bulk_loader.SOURCE_TLS_CA_DEFAULT, '/app/source-ca.pem')

    def test_the_task_definition_sets_source_tls(self):
        path = os.path.join(os.path.dirname(os.path.dirname(LOADER)), 'infra',
                            'ecdl-loader-ecs.yaml')
        with open(path) as handle:
            text = handle.read()
        self.assertIn('- { Name: SOURCE_TLS, Value: !Ref SourceTLS }', text)
        self.assertRegex(text, r"SourceTLS:\n    Type: String\n    Default: 'true'")


class MainTest(_Env, unittest.TestCase):
    def test_the_log_says_how_the_source_is_reached(self):
        from tests.fake_source import FakeSource, run_main
        FakeSource.reset({'public.t': {'pk': ['id'], 'columns': [('id', 'int')], 'rows': []}})
        target = {'host': '127.0.0.1', 'port': 1, 'ssl': False, 'cluster': False}
        out = run_main(target, 't')
        self.assertIn('source TLS: OFF (SOURCE_TLS=false)', out)


if __name__ == '__main__':
    unittest.main()
