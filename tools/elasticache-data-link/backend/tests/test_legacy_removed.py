"""The superseded package, deploy and sink-template code stays deleted (Jira story F147).

Those endpoints were behind ECDL_LEGACY_PACKAGES. Behind that one variable they
ran shell commands on any instance through SSM with request text in them,
launched EC2 instances with the database password in the user data, used an S3
bucket name anyone could register first, read sink templates by a path taken
from the request, and shipped sink templates that could lose data. Links
replace all of it, so the code was removed rather than kept switched off.

These tests check that the routes are gone (and that the old variable does not
bring them back), that what the UI still uses is present, and that every API
path the UI calls is one the backend serves.

Run with:  python3 tests/test_legacy_removed.py     (from the backend/ directory)
"""
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_BACKEND)
sys.path.insert(0, _BACKEND)
sys.path.insert(0, _ROOT)

REMOVED = (
    ('GET', '/api/pipelines/1/generate-config'),
    ('GET', '/api/pipelines/1/generate-docker-compose'),
    ('GET', '/api/pipelines/1/download-deployment'),
    ('GET', '/api/pipelines/1/generate-ecs-fargate'),
    ('GET', '/api/pipelines/1/download-ecs-deployment'),
    ('POST', '/api/pipelines/1/deploy-local'),
    ('GET', '/api/pipelines/1/deploy-local/status'),
    ('POST', '/api/pipelines/1/deploy-cloud'),
    ('POST', '/api/connectors/1/apply-changes'),
    ('POST', '/api/smart-builder/chat'),
    ('GET', '/api/sink-templates'),
    # Review finding H10: the superseded object model and the Randomizer.
    ('GET', '/api/sources'),
    ('GET', '/api/sources/1/scan-tables'),
    ('GET', '/api/sources/1/tables'),
    ('POST', '/api/randomizer/start'),
    ('GET', '/api/connectors'),
    ('GET', '/api/brokers'),
    ('GET', '/api/targets'),
    ('GET', '/api/pipelines'),
    ('GET', '/api/deployments'),
)

# What the current UI and CLI still use. Not exhaustive; one per area, so a
# deletion that went too far fails here.
KEPT = (
    ('GET', '/api/auth/status'),
    ('GET', '/api/health'),
    ('GET', '/api/maintenance/backup'),
    ('POST', '/api/links/deploy'),
    ('POST', '/api/links/destroy'),
    ('GET', '/api/links/discover'),
    ('GET', '/api/monitor/dashboard'),
)

# Calls and names that only the removed code made.
FORBIDDEN_IN_SOURCE = ('run_instances', 'send_command', 'create_bucket',
                       'ECDL_LEGACY_PACKAGES', "'templates', 'sink'",
                       'bedrock-runtime')


def _app():
    import importlib
    import app as app_module
    importlib.reload(app_module)
    app_module.app.testing = True
    return app_module.app


def _matches(app, method, path):
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    adapter = app.url_map.bind('localhost')
    try:
        adapter.match(path, method=method)
        return True
    except MethodNotAllowed:
        return method is None
    except NotFound:
        return False


class RemovedTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ('ECDL_LEGACY_PACKAGES', 'ECDL_AUTH_DISABLED')}
        os.environ['ECDL_AUTH_DISABLED'] = 'true'

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_removed_routes_are_not_served(self):
        app = _app()
        for method, path in REMOVED:
            self.assertFalse(_matches(app, None, path), path)
            res = app.test_client().open(path, method=method)
            self.assertEqual(res.status_code, 404, path)

    def test_the_old_switch_does_not_bring_them_back(self):
        os.environ['ECDL_LEGACY_PACKAGES'] = 'true'
        app = _app()
        for method, path in REMOVED:
            self.assertFalse(_matches(app, None, path), path)

    def test_what_the_ui_uses_is_still_served(self):
        app = _app()
        for method, path in KEPT:
            self.assertTrue(_matches(app, method, path), '{} {}'.format(method, path))

    def test_sink_templates_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(_BACKEND, 'templates')))

    def test_no_removed_call_is_left_in_the_backend(self):
        for name in ('app.py', 'links.py', 'monitor.py', 'auth.py'):
            with open(os.path.join(_BACKEND, name)) as handle:
                text = handle.read()
            for needle in FORBIDDEN_IN_SOURCE:
                self.assertNotIn(needle, text, '{} in {}'.format(needle, name))

    def test_compose_no_longer_mounts_the_templates(self):
        with open(os.path.join(_ROOT, 'docker-compose.yml')) as handle:
            self.assertNotIn('templates', handle.read())


# `${API_URL}/api/...` (or ${apiUrl}) in the UI sources, up to the closing
# backtick, a quote or the query string.
_UI_CALL = re.compile(r'\$\{(?:API_URL|apiUrl)\}(/api/[^`\'"\s?]*)')
# Links.js posts to `/api/links/${path}`, where path is the second argument of
# its call(what, path, body) helper.
_LINKS_CALL = re.compile(r"\bcall\('[^']*',\s*'([a-z/-]+)'")


def _concrete(raw):
    """A path a placeholder-bearing template string would produce."""
    out = ''
    rest = raw
    while '${' in rest:
        before, after = rest.split('${', 1)
        out += before
        inner = after.split('}', 1)[0]
        if '}' not in after or '?' in inner or '`' in inner:
            # A conditional query string (`${x ? `?a=${x}` : ''}`): the path
            # ends here.
            return out
        out += '1'
        rest = after.split('}', 1)[1] if '}' in after else ''
    return out + rest


def _ui_paths():
    src = os.path.join(_ROOT, 'frontend', 'src')
    found = {}
    for folder, _dirs, files in os.walk(src):
        for name in files:
            if not name.endswith('.js') or name.endswith('.test.js'):
                continue
            path = os.path.join(folder, name)
            with open(path) as handle:
                text = handle.read()
            where = os.path.relpath(path, _ROOT)
            for match in _UI_CALL.finditer(text):
                raw = match.group(1)
                if raw == '/api/links/${path}':
                    continue                  # expanded from the call sites below
                found.setdefault(_concrete(raw), where)
            if '/api/links/${path}' in text:
                for action in _LINKS_CALL.findall(text):
                    found.setdefault('/api/links/' + action, where)
    return found


class UiWiringTest(unittest.TestCase):
    """Every API path the UI calls is served, so nothing calls a deleted route."""

    def test_ui_calls_only_routes_that_exist(self):
        if not os.path.isdir(os.path.join(_ROOT, 'frontend', 'src')):
            raise unittest.SkipTest('frontend sources not present')
        os.environ.setdefault('ECDL_AUTH_DISABLED', 'true')
        app = _app()
        paths = _ui_paths()
        self.assertGreater(len(paths), 20, 'the scan found too few calls')
        missing = sorted('{} ({})'.format(p, f) for p, f in paths.items()
                         if not _matches(app, None, p))
        self.assertEqual(missing, [])

    def test_the_scan_would_notice_a_removed_route(self):
        # Guards the test above: a removed path must not match.
        app = _app()
        self.assertFalse(_matches(app, None, '/api/pipelines/1/deploy-cloud'))
        paths = _ui_paths()
        self.assertIn('/api/links/deploy', paths)
        self.assertIn('/api/links/plan', paths)          # via call('plan', 'plan')
        self.assertIn('/api/monitor/dashboard', paths)   # query string cut off
        self.assertIn('/api/monitor/logs/streams', paths)  # ${apiUrl} form

    def test_no_ui_call_bypasses_the_scan(self):
        # The scan above only sees `${API_URL}/api/...`. A quoted path written
        # without it ('/api/x', href="/api/x") would call the backend unseen.
        bare = re.compile(r'''[`'"](/api/[^`'"\s]*)''')
        src = os.path.join(_ROOT, 'frontend', 'src')
        if not os.path.isdir(src):
            raise unittest.SkipTest('frontend sources not present')
        found = []
        for folder, _dirs, files in os.walk(src):
            for name in files:
                if not name.endswith('.js') or name.endswith('.test.js'):
                    continue
                with open(os.path.join(folder, name)) as handle:
                    for number, line in enumerate(handle, 1):
                        found += ['{}:{} {}'.format(name, number, m)
                                  for m in bare.findall(line)]
        self.assertEqual(found, [])
    def test_legacy_deployments_page_only_lists(self):
        # Jira story F190: its generate and download buttons called routes deleted in
        # Jira story F147, and a download of an error body was saved as a zip. The page is
        # now a read-only list: one GET, no buttons, no downloads.
        path = os.path.join(_ROOT, 'frontend', 'src', 'components', 'Deployments.js')
        if not os.path.exists(path):
            raise unittest.SkipTest('frontend sources not present')
        with open(path) as handle:
            text = handle.read()
        self.assertEqual(_UI_CALL.findall(text), ['/api/deployments'])
        for needle in ('<button', 'onClick', 'download', 'href=', 'window.open',
                       'createObjectURL', 'axios.post', 'fetch('):
            self.assertNotIn(needle, text, needle)
    def test_placeholders_become_segments(self):
        self.assertEqual(_concrete('/api/sources/${src.id}/tables'),
                         '/api/sources/1/tables')
        self.assertEqual(_concrete("/api/monitor/dashboard${w ? `?link=${w}` : ''}"),
                         '/api/monitor/dashboard')
        # As _UI_CALL captures it: cut at the space, so the placeholder is open.
        self.assertEqual(_concrete('/api/monitor/dashboard${watching'),
                         '/api/monitor/dashboard')


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} legacy-removal tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)
