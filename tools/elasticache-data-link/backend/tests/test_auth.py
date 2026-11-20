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
             'ECDL_AUTH_DISABLED', 'ECDL_AUTH_READS',
             'ECDL_ALLOWED_HOSTS', 'ECDL_ALLOWED_ORIGINS')


def make_app(**env):
    """A minimal app with auth installed and one route per method shape."""
    from flask import Flask, jsonify
    app = Flask(__name__)
    auth.install(app)
    calls = []   # endpoints whose view actually ran

    @app.route('/read', methods=['GET'])
    def read():
        calls.append('read')
        return jsonify({'read': True})

    @app.route('/write', methods=['POST'])
    def write():
        calls.append('write')
        return jsonify({'wrote': True})

    @app.route('/remove', methods=['DELETE'])
    def remove():
        return jsonify({'removed': True})

    @app.route('/always', methods=['GET'])
    @auth.require_token
    def always():
        return jsonify({'ok': True})

    app.testing = True  # Flask's test mode for the test client (same as config TESTING)
    client = app.test_client()
    client.view_calls = calls
    return client


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


class HostCheckTest(AuthTestCase):
    """The Host allowlist (Jira story F146), which stops DNS rebinding.

    A rebinding page is served from the attacker's domain and sends that domain
    as the Host, even though the connection reaches this API. So the Host is the
    one thing the attacker cannot choose, and it has to be checked in every
    AccessMode, not only ssm.
    """

    def setUp(self):
        super().setUp()
        # Auth off, as in every shipped setup: the host check is then the only
        # thing in the way, which is the case that matters.
        os.environ['ECDL_AUTH_DISABLED'] = 'true'

    def get(self, host, path='/read', method='get', **kw):
        client = make_app()
        if host is None:
            # A request with no Host header at all (HTTP/1.0 style).
            client.application.wsgi_app = _DropHost(client.application.wsgi_app)
            res = getattr(client, method)(path, **kw)
        else:
            res = getattr(client, method)(path, headers={'Host': host}, **kw)
        return res, client.view_calls

    def assertAllowed(self, host, **kw):
        res, calls = self.get(host, **kw)
        self.assertEqual(res.status_code, 200, '{!r}: {}'.format(host, res.data))
        self.assertEqual(len(calls), 1)

    def assertRefused(self, host, **kw):
        res, calls = self.get(host, **kw)
        self.assertEqual(res.status_code, 403, '{!r}: {}'.format(host, res.data))
        self.assertIn('not accepted', json.loads(res.data)['error'])
        self.assertEqual(calls, [], 'the route ran for a refused host')

    # -- nothing configured: loopback only (docker compose, a bare python app.py)

    def test_unset_allows_loopback_names_on_any_port(self):
        for host in ('localhost', 'localhost:5006', '127.0.0.1', '127.0.0.1:5000',
                     '[::1]', '[::1]:5000'):
            self.assertAllowed(host)

    def test_unset_refuses_any_other_name(self):
        for host in ('evil.example', 'evil.example:5000', '10.0.0.5:5000',
                     'localhost.evil.example', '127.0.0.1.nip.io:5000',
                     '0.0.0.0:5000', '[::2]:5000'):
            self.assertRefused(host)

    def test_empty_value_is_loopback_only_not_any(self):
        # It used to mean "answer any host", which is the open default this fixes.
        os.environ['ECDL_ALLOWED_HOSTS'] = ''
        self.assertAllowed('localhost:5000')
        self.assertRefused('evil.example:5000')
        os.environ['ECDL_ALLOWED_HOSTS'] = ' , '
        self.assertRefused('evil.example:5000')

    def test_missing_or_empty_host_header_is_refused(self):
        self.assertRefused(None)
        self.assertRefused('')

    def test_malformed_hosts_are_refused(self):
        for host in ('localhost:', 'localhost:abc', 'localhost:5000:1',
                     '[::1', '[::1]x', '[]:5000', ':5000', 'localhost:123456'):
            self.assertRefused(host)

    def test_case_does_not_matter(self):
        self.assertAllowed('LOCALHOST:5000')
        os.environ['ECDL_ALLOWED_HOSTS'] = 'UI.Example.COM'
        self.assertAllowed('ui.example.com')
        self.assertAllowed('Ui.Example.Com:443')

    # -- ssm mode, as the template sets it: exact host:port

    def test_ssm_list_allows_only_the_tunnel_port(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = 'localhost:5000,127.0.0.1:5000'
        self.assertAllowed('localhost:5000')
        self.assertAllowed('127.0.0.1:5000')
        self.assertRefused('localhost:3000')
        self.assertRefused('localhost')
        self.assertRefused('[::1]:5000')
        self.assertRefused('evil.example:5000')

    # -- cidr / load-balancer mode, as the template builds it from IMDS

    def test_cidr_list_allows_the_instance_addresses(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = (
            'localhost,127.0.0.1,[::1],10.0.1.23,ip-10-0-1-23.ec2.internal,'
            '203.0.113.9,ec2-203-0-113-9.compute-1.amazonaws.com,ecdl.example.com')
        for host in ('10.0.1.23:5000', 'ip-10-0-1-23.ec2.internal:5000',
                     '203.0.113.9:5000', 'ecdl.example.com',
                     'ec2-203-0-113-9.compute-1.amazonaws.com:5000',
                     'localhost:5000'):
            self.assertAllowed(host)
        for host in ('10.0.1.24:5000', 'evil.example:5000',
                     'ecdl.example.com.evil.example', 'sub.ecdl.example.com'):
            self.assertRefused(host)

    def test_ipv6_entries_with_and_without_brackets(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = '[fd00::5],fd00::6,[fd00::7]:5000'
        self.assertAllowed('[fd00::5]:5000')
        self.assertAllowed('[FD00::6]:1234')
        self.assertAllowed('[fd00::7]:5000')
        self.assertRefused('[fd00::7]:5001')
        self.assertRefused('[fd00::8]:5000')

    def test_every_method_is_checked_including_preflight(self):
        self.assertRefused('evil.example', path='/write', method='post')
        self.assertRefused('evil.example', path='/remove', method='delete')
        res, calls = self.get('evil.example', path='/write', method='options')
        self.assertEqual(res.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', res.headers)

    def test_the_host_check_holds_with_auth_on_and_a_good_token(self):
        # A token does not license an unknown Host; a rebinding page could not
        # hold one anyway, but the order of the checks should not depend on it.
        os.environ.pop('ECDL_AUTH_DISABLED')
        os.environ['ECDL_API_TOKEN'] = 'tok'  # nosec B105 - fake test credential
        client = make_app()
        res = client.post('/write', headers={'Host': 'evil.example',
                                             'Authorization': 'Bearer tok'})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(client.view_calls, [])
        res = client.post('/write', headers={'Host': 'localhost',
                                             'Authorization': 'Bearer tok'})
        self.assertEqual(res.status_code, 200)

    def test_refusal_names_the_allowed_hosts(self):
        res, _ = self.get('evil.example')
        body = json.loads(res.data)
        self.assertIn('localhost', body['hint'])
        self.assertIn('ECDL_ALLOWED_HOSTS', body['hint'])


class _DropHost:
    """WSGI wrapper that removes the Host header before Flask sees it."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        environ.pop('HTTP_HOST', None)
        return self.app(environ, start_response)


class RealAppHostTest(AuthTestCase):
    """Deploy, destroy and discover on the real app refuse an unknown Host."""

    ROUTES = (('post', '/api/links/deploy'), ('post', '/api/links/destroy'),
              ('get', '/api/links/discover'), ('post', '/api/links/discover/tables'),
              ('get', '/api/links/deployments'), ('post', '/api/links/repair'))

    def setUp(self):
        super().setUp()
        os.environ['ECDL_AUTH_DISABLED'] = 'true'
        try:
            import importlib
            import app as app_module
            importlib.reload(app_module)
        except Exception as exc:                          # noqa: BLE001
            raise unittest.SkipTest('app not importable here: {}'.format(exc))
        self.app = app_module.app
        self.app.testing = True
        # Replace the views with recorders, so the test proves the view did not
        # run (and nothing reaches AWS when it would have).
        self.ran = []
        for rule in self.app.url_map.iter_rules():
            if any(rule.rule == path for _, path in self.ROUTES):
                self.app.view_functions[rule.endpoint] = self._recorder(rule.rule)

    def _recorder(self, path):
        def view(*args, **kwargs):
            self.ran.append(path)
            return '{"ran": true}', 200, {'Content-Type': 'application/json'}
        return view

    def test_routes_refuse_a_rebinding_host(self):
        client = self.app.test_client()
        for method, path in self.ROUTES:
            res = getattr(client, method)(path, headers={
                'Host': 'attacker.example:5000',
                'Origin': 'http://attacker.example:5000'})
            self.assertEqual(res.status_code, 403, path)
            self.assertIn('not accepted', json.loads(res.data)['error'])
        self.assertEqual(self.ran, [])

    def test_routes_still_answer_loopback(self):
        client = self.app.test_client()
        for method, path in self.ROUTES:
            res = getattr(client, method)(path, headers={'Host': 'localhost:5006'})
            self.assertEqual(res.status_code, 200, path)
        self.assertEqual(sorted(self.ran), sorted(p for _, p in self.ROUTES))

    def test_routes_follow_a_configured_list(self):
        os.environ['ECDL_ALLOWED_HOSTS'] = 'localhost:5000,127.0.0.1:5000'
        client = self.app.test_client()
        ok = client.post('/api/links/deploy', headers={'Host': '127.0.0.1:5000'})
        bad = client.post('/api/links/deploy', headers={'Host': '127.0.0.1:5006'})
        self.assertEqual((ok.status_code, bad.status_code), (200, 403))
        self.assertEqual(self.ran, ['/api/links/deploy'])


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
