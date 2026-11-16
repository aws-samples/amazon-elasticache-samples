"""Tests for the loader's target-client factory and credential handling.

Run with:  python3 data-plane/bulk-loader/test_target_client.py

The loader and the Go sink write to the same cache, so their credential rules are
deliberately identical -- a secret that worked for one and not the other would be
a confusing partial failure. These tests check the shared behaviour on the Python side;
tools/go-sink/connect_test.go checks the same cases on the Go side.

Stdlib only, and the valkey package is stubbed so this runs without it installed.
"""

import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.dirname(HERE)          # data-plane/bulk-loader
sys.path.insert(0, LOADER)


def _install_valkey_stub():
    """Record which client class the factory chose, without needing the library."""
    created = []

    class FakeCluster(object):
        def __init__(self, **kwargs):
            created.append(('cluster', kwargs))
            self.kwargs = kwargs

    class FakeSingle(object):
        def __init__(self, **kwargs):
            created.append(('single', kwargs))
            self.kwargs = kwargs

    valkey = types.ModuleType('valkey')
    valkey.Valkey = FakeSingle
    cluster_mod = types.ModuleType('valkey.cluster')
    cluster_mod.ValkeyCluster = FakeCluster
    valkey.cluster = cluster_mod
    sys.modules['valkey'] = valkey
    sys.modules['valkey.cluster'] = cluster_mod
    return created


CREATED = _install_valkey_stub()

# pymysql/psycopg2/pymssql are imported lazily inside the adapters, so importing
# the module itself needs nothing but the stub above.
import bulk_loader  # noqa: E402


def clear_env():
    for key in ('VALKEY_USERNAME', 'VALKEY_PASSWORD', 'VALKEY_SECRET'):
        os.environ.pop(key, None)


class ClientTopologyTest(unittest.TestCase):
    """Both topologies must work (see target_client() in bulk_loader.py)."""

    def setUp(self):
        del CREATED[:]

    def test_cluster_mode_uses_the_cluster_client(self):
        bulk_loader.target_client({'host': 'h', 'cluster': True})
        self.assertEqual([kind for kind, _ in CREATED], ['cluster'])

    def test_non_cluster_mode_uses_the_plain_client(self):
        bulk_loader.target_client({'host': 'h', 'cluster': False})
        self.assertEqual([kind for kind, _ in CREATED], ['single'])

    def test_defaults_to_cluster_for_existing_callers(self):
        # Callers that predate this flag all point at cluster-mode targets.
        bulk_loader.target_client({'host': 'h'})
        self.assertEqual([kind for kind, _ in CREATED], ['cluster'])

    def test_connection_settings_passed_through(self):
        bulk_loader.target_client({'host': 'cache', 'port': 7000, 'ssl': False,
                                   'cluster': True})
        _, kwargs = CREATED[0]
        self.assertEqual(kwargs['host'], 'cache')
        self.assertEqual(kwargs['port'], 7000)
        self.assertFalse(kwargs['ssl'])
        self.assertFalse(kwargs['decode_responses'],
                         'the loader writes bytes, not decoded strings')

    def test_tls_defaults_on(self):
        bulk_loader.target_client({'host': 'h'})
        self.assertTrue(CREATED[0][1]['ssl'])

    def test_tls_verifies_the_certificate_and_hostname(self):
        # ssl_cert_reqs=None (the old value) accepted any certificate at all.
        bulk_loader.target_client({'host': 'h'})
        _, kwargs = CREATED[0]
        self.assertEqual(kwargs['ssl_cert_reqs'], 'required')
        self.assertTrue(kwargs['ssl_check_hostname'])

    def test_no_credential_means_no_auth_kwargs(self):
        bulk_loader.target_client({'host': 'h'})
        _, kwargs = CREATED[0]
        self.assertNotIn('password', kwargs)
        self.assertNotIn('username', kwargs)

    def test_auth_token_sets_password_only(self):
        bulk_loader.target_client({'host': 'h', 'password': 'tok'})  # nosec B105 - fake test credential
        _, kwargs = CREATED[0]
        self.assertEqual(kwargs['password'], 'tok')
        self.assertNotIn('username', kwargs,
                         'an auth token has no username')

    def test_rbac_sets_both(self):
        bulk_loader.target_client({'host': 'h', 'username': 'u',
                                   'password': 'p'})  # nosec B105 - fake test credential
        _, kwargs = CREATED[0]
        self.assertEqual((kwargs['username'], kwargs['password']), ('u', 'p'))

    def test_username_without_password_is_ignored(self):
        # A username alone cannot authenticate, and passing it would produce a
        # confusing driver error rather than the accurate "no credential".
        bulk_loader.target_client({'host': 'h', 'username': 'u'})
        _, kwargs = CREATED[0]
        self.assertNotIn('username', kwargs)
        self.assertNotIn('password', kwargs)

    def test_auth_works_on_both_topologies(self):
        for clustered, expected in ((True, 'cluster'), (False, 'single')):
            del CREATED[:]
            bulk_loader.target_client({'host': 'h', 'cluster': clustered,
                                       'username': 'u', 'password': 'p'})  # nosec B105 - fake test credential
            kind, kwargs = CREATED[0]
            self.assertEqual(kind, expected)
            self.assertEqual(kwargs['username'], 'u')
            self.assertEqual(kwargs['password'], 'p')


class CredentialFromEnvTest(unittest.TestCase):
    """Mirrors tools/go-sink/connect.go loadCredential case for case."""

    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_nothing_configured(self):
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': '', 'password': ''})  # nosec B105 - fake test credential

    def test_plaintext_secret_is_a_token(self):
        os.environ['VALKEY_SECRET'] = 'tok3n'  # nosec B105 - fake test credential
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': '', 'password': 'tok3n'})  # nosec B105 - fake test credential

    def test_json_secret_is_rbac(self):
        os.environ['VALKEY_SECRET'] = '{"username":"ecdl","password":"p@ss w0rd"}'  # nosec B105 - fake test credential
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': 'ecdl', 'password': 'p@ss w0rd'})  # nosec B105 - fake test credential

    def test_json_secret_with_extra_fields(self):
        os.environ['VALKEY_SECRET'] = (  # nosec B105 - fake test credential
            '{"engine":"valkey","username":"u","password":"p","port":6379}')
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': 'u', 'password': 'p'})  # nosec B105 - fake test credential

    def test_json_without_password_is_fatal(self):
        os.environ['VALKEY_SECRET'] = '{"username":"only"}'  # nosec B105 - fake test credential
        with self.assertRaises(SystemExit) as ctx:
            bulk_loader.target_credential_from_env()
        self.assertIn('password', str(ctx.exception))

    def test_token_containing_a_brace_is_not_misread(self):
        os.environ['VALKEY_SECRET'] = '{not-json-at-all'  # nosec B105 - fake test credential
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': '', 'password': '{not-json-at-all'})  # nosec B105 - fake test credential

    def test_explicit_vars_win(self):
        os.environ['VALKEY_USERNAME'] = 'explicit'
        os.environ['VALKEY_PASSWORD'] = 'explicit-pass'  # nosec B105 - fake test credential
        os.environ['VALKEY_SECRET'] = '{"username":"secret","password":"x"}'  # nosec B105 - fake test credential
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': 'explicit', 'password': 'explicit-pass'})  # nosec B105 - fake test credential

    def test_password_only_explicit_is_a_token(self):
        os.environ['VALKEY_PASSWORD'] = 'tok'  # nosec B105 - fake test credential
        self.assertEqual(bulk_loader.target_credential_from_env(),
                         {'username': '', 'password': 'tok'})  # nosec B105 - fake test credential


class NoHardcodedClusterClientTest(unittest.TestCase):

    def test_cluster_client_is_only_constructed_in_the_factory(self):
        """Guards the regression: two call sites used to hardcode ValkeyCluster.

        Both are now routed through target_client(), so a non-sharded target
        works. If a third call site appears, this catches it.
        """
        with open(os.path.join(LOADER, 'bulk_loader.py')) as fh:
            lines = fh.readlines()
        offenders = [
            (i + 1, line.strip()) for i, line in enumerate(lines)
            if 'ValkeyCluster(' in line
        ]
        self.assertEqual(
            len(offenders), 1,
            'ValkeyCluster should be constructed once, inside target_client(); '
            'found: {}'.format(offenders))


if __name__ == '__main__':
    unittest.main(verbosity=2)
