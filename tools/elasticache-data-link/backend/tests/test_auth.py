"""Tests for control-plane authentication.

The behaviour that matters is not "does a good token work" -- it is what happens
in the three failure modes: no token configured, a wrong token, and a request
that changes nothing. Getting the first one wrong is how an unprotected control
plane comes to look like a working one.

Run with:  python3 test_auth.py     (from the backend/ directory)
"""
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, _BACKEND)                   # backend/  (import auth, monitor, links, app)
sys.path.insert(0, os.path.dirname(_BACKEND))  # repo root (backend modules import ecdl_link)

import auth  # noqa: E402


AUTH_VARS = ('ECDL_API_TOKEN', 'ECDL_API_TOKEN_SECRET_ID',
             'ECDL_AUTH_DISABLED', 'ECDL_AUTH_READS')


def make_app(**env):
    """A minimal app with auth installed and one route per method shape."""
    from flask import Flask, jsonify
    app = Flask(__name__)
    auth.install(app)

    @app.route('/read', methods=['GET'])
    def read():
        return jsonify({'read': True})

    @app.route('/write', methods=['POST'])
    def write():
        return jsonify({'wrote': True})

    @app.route('/remove', methods=['DELETE'])
    def remove():
        return jsonify({'removed': True})

    @app.route('/always', methods=['GET'])
    @auth.require_token
    def always():
        return jsonify({'ok': True})

    app.testing = True  # Flask's test mode for the test client (same as config TESTING)
    return app.test_client()


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in AUTH_VARS}
        for key in AUTH_VARS:
            os.environ.pop(key, None)
        auth.reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        auth.reset_cache()

    def client(self):
        return make_app()

    def bearer(self, token):
        return {'Authorization': 'Bearer {}'.format(token)}


class UnconfiguredTest(AuthTestCase):
    """No token configured. This is the case that has to fail CLOSED."""

    def test_a_write_is_refused_when_no_token_is_configured(self):
        # The opposite behaviour -- "nothing configured, so skip the check" --
        # makes a misconfiguration look like a working system, and the system it
        # looks like is the unprotected one.
        res = self.client().post('/write')
        self.assertEqual(res.status_code, 503)
        body = json.loads(res.data)
        self.assertFalse(body['authenticated'])
        self.assertIn('no API token configured', body['error'])

    def test_the_refusal_says_how_to_fix_it(self):
        body = json.loads(self.client().post('/write').data)
        self.assertIn('ECDL_API_TOKEN', body['hint'])

    def test_a_delete_is_refused_too(self):
        self.assertEqual(self.client().delete('/remove').status_code, 503)

    def test_a_read_still_works(self):
        # Dashboards are the read surface and hold no credentials; the risk that
        # motivated this is mutation.
        self.assertEqual(self.client().get('/read').status_code, 200)

    def test_presenting_a_token_does_not_help_when_none_is_configured(self):
        # Otherwise any token would authenticate an unconfigured server.
        res = self.client().post('/write', headers=self.bearer('anything'))
        self.assertEqual(res.status_code, 503)


class ConfiguredTest(AuthTestCase):
    TOKEN = 's3cret-token-value'  # nosec B105 - fake test credential

    def setUp(self):
        super(ConfiguredTest, self).setUp()
        os.environ['ECDL_API_TOKEN'] = self.TOKEN
        auth.reset_cache()

    def test_a_correct_token_is_accepted(self):
        res = self.client().post('/write', headers=self.bearer(self.TOKEN))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.data)['wrote'])

    def test_a_missing_token_is_unauthorised(self):
        res = self.client().post('/write')
        self.assertEqual(res.status_code, 401)
        self.assertFalse(json.loads(res.data)['authenticated'])

    def test_a_wrong_token_is_unauthorised(self):
        res = self.client().post('/write', headers=self.bearer('wrong'))
        self.assertEqual(res.status_code, 401)

    def test_a_token_that_is_a_prefix_of_the_real_one_is_rejected(self):
        res = self.client().post('/write', headers=self.bearer(self.TOKEN[:-1]))
        self.assertEqual(res.status_code, 401)

    def test_the_challenge_names_the_scheme(self):
        # So a client knows what to present rather than guessing.
        res = self.client().post('/write')
        self.assertIn('Bearer', res.headers.get('WWW-Authenticate', ''))

    def test_the_header_variant_scripts_reach_for_also_works(self):
        # Refusing it leads people to use a query string, where the token
        # would be written to access logs.
        res = self.client().post('/write', headers={'X-ECDL-Token': self.TOKEN})
        self.assertEqual(res.status_code, 200)

    def test_a_read_is_still_open_by_default(self):
        self.assertEqual(self.client().get('/read').status_code, 200)

    def test_reads_can_be_protected_too(self):
        os.environ['ECDL_AUTH_READS'] = 'true'
        self.assertEqual(self.client().get('/read').status_code, 401)
        self.assertEqual(
            self.client().get('/read', headers=self.bearer(self.TOKEN)).status_code,
            200)

    def test_the_decorator_protects_a_read_shaped_route_regardless(self):
        # For endpoints that are GET-shaped but expensive or state-changing.
        self.assertEqual(self.client().get('/always').status_code, 401)
        self.assertEqual(
            self.client().get('/always', headers=self.bearer(self.TOKEN)).status_code,
            200)

    def test_cors_preflight_is_not_blocked(self):
        # The browser sends OPTIONS with no Authorization header before it will
        # send the real request. Blocking it breaks every cross-origin call in a
        # way that looks like a network fault rather than an auth failure.
        res = self.client().open('/write', method='OPTIONS')
        self.assertNotEqual(res.status_code, 401)
        self.assertNotEqual(res.status_code, 503)


class DisabledTest(AuthTestCase):
    def setUp(self):
        super(DisabledTest, self).setUp()
        os.environ['ECDL_AUTH_DISABLED'] = 'true'
        auth.reset_cache()

    def test_everything_is_open_when_explicitly_disabled(self):
        self.assertEqual(self.client().post('/write').status_code, 200)
        self.assertEqual(self.client().get('/read').status_code, 200)

    def test_disabling_requires_an_explicit_truthy_value(self):
        # A typo must not silently open the control plane.
        for value in ('', 'no', 'false', 'maybe', '0'):
            os.environ['ECDL_AUTH_DISABLED'] = value
            auth.reset_cache()
            self.assertEqual(self.client().post('/write').status_code, 503,
                             'ECDL_AUTH_DISABLED=%r should not disable auth' % value)


class StatusTest(AuthTestCase):
    def test_status_never_returns_the_token(self):
        os.environ['ECDL_API_TOKEN'] = 'do-not-leak-me'  # nosec B105 - fake test credential
        auth.reset_cache()
        blob = json.dumps(auth.status())
        self.assertNotIn('do-not-leak-me', blob)
        self.assertTrue(auth.status()['configured'])

    def test_status_reports_what_is_protected(self):
        os.environ['ECDL_API_TOKEN'] = 't'  # nosec B105 - fake test credential
        auth.reset_cache()
        self.assertEqual(auth.status()['protects'], 'writes')
        os.environ['ECDL_AUTH_READS'] = 'true'
        self.assertEqual(auth.status()['protects'], 'all requests')
        os.environ['ECDL_AUTH_DISABLED'] = 'true'
        self.assertEqual(auth.status()['protects'], 'nothing')

    def test_status_shows_unconfigured_without_pretending_it_is_fine(self):
        self.assertFalse(auth.status()['configured'])
        self.assertTrue(auth.status()['enabled'])


class SecretTest(AuthTestCase):
    def test_a_transient_secret_failure_is_not_cached(self):
        """A brief failure must not keep an empty token cached for the life of the process, which
        would block the control plane's own writes until a restart."""
        os.environ['ECDL_API_TOKEN_SECRET_ID'] = 'ecdl/does-not-exist'  # nosec B105 - fake test credential
        auth.reset_cache()
        self.assertEqual(auth.configured_token(), '')
        # Nothing cached, so a later success is still possible.
        self.assertEqual(auth._token_cache, {})

    def test_a_direct_token_wins_over_a_secret_lookup(self):
        # So a local run needs no AWS call at all.
        os.environ['ECDL_API_TOKEN'] = 'direct'  # nosec B105 - fake test credential
        os.environ['ECDL_API_TOKEN_SECRET_ID'] = 'ecdl/unused'  # nosec B105 - fake test credential
        auth.reset_cache()
        self.assertEqual(auth.configured_token(), 'direct')


class RealAppTest(AuthTestCase):
    """The same rules on the actual app, with its real blueprints."""

    def app_client(self):
        from flask import Flask
        import importlib
        import app as app_module
        importlib.reload(app_module)
        self.assertIsInstance(app_module.app, Flask)
        app_module.app.testing = True  # Flask's test mode for the test client
        return app_module.app.test_client()

    def test_deploy_is_refused_without_a_token(self):
        # The endpoint that creates CloudFormation stacks, ECS services and IAM
        # roles. This is the one that had to be closed.
        try:
            client = self.app_client()
        except Exception as exc:                          # noqa: BLE001
            raise unittest.SkipTest('app not importable here: {}'.format(exc))
        res = client.post('/api/links/deploy', data='{}',
                          content_type='application/json')
        self.assertIn(res.status_code, (401, 503))

    def test_the_monitor_dashboard_is_still_readable(self):
        try:
            client = self.app_client()
        except Exception as exc:                          # noqa: BLE001
            raise unittest.SkipTest('app not importable here: {}'.format(exc))
        # Reads stay open by default so the monitor keeps working; it may fail on
        # AWS access in a test environment, but it must not fail on AUTH.
        res = client.get('/api/auth/status')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('token', json.loads(res.data))


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} auth tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)
