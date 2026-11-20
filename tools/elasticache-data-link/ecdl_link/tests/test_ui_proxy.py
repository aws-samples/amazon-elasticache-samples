"""The UI forwards /api/* to the backend, so the page and the API share one
origin in every AccessMode (Jira story F155).

In cidr mode the page at <ip>:3000 called the API at <ip>:5000, an origin the
backend's CORS allowlist did not name, so the browser blocked every call. The
UI's nginx now forwards /api/* to the backend container over a private docker
network. These tests cover the three pieces: the user data that runs the
containers, the entrypoint that writes the forwarding settings, and the nginx
configuration that uses them.
"""
import os
import re
import shutil
import subprocess  # nosec B404 - runs sh on the repo's own entrypoint script
import tempfile
import unittest

from ecdl_link.tests.test_control_plane_iam import _template, ROOT

FRONTEND = os.path.join(ROOT, 'frontend')
ENTRYPOINT = os.path.join(FRONTEND, 'docker-entrypoint.d', '40-ecdl-config.sh')
NGINX_CONF = os.path.join(FRONTEND, 'nginx', 'nginx.conf')
DOCKERFILE = os.path.join(FRONTEND, 'Dockerfile.prod')


def _read(path):
    with open(path) as handle:
        return handle.read()


def _user_data():
    sub = _template()['Resources']['Instance']['Properties']['UserData']['Fn::Base64']['Sub']
    return sub[0], sub[1]


def _docker_run(script, name):
    """The `docker run` command for container `name`, joined onto one line."""
    start = script.index('docker run -d --name ' + name)
    lines = []
    for line in script[start:].splitlines():
        lines.append(line.strip().rstrip('\\').strip())
        if not line.rstrip().endswith('\\'):
            break
    return ' '.join(lines)


class UserDataTest(unittest.TestCase):

    def test_both_containers_share_a_private_network(self):
        script, _ = _user_data()
        create = script.index('docker network create ecdl')
        self.assertLess(create, script.index('docker run -d --name ecdl-backend'))
        for name in ('ecdl-backend', 'ecdl-frontend'):
            self.assertIn('--network ecdl', _docker_run(script, name), name)

    def test_the_ui_forwards_to_the_backend_container_and_uses_its_own_origin(self):
        script, subs = _user_data()
        ui = _docker_run(script, 'ecdl-frontend')
        self.assertIn('-e ECDL_API_UPSTREAM=ecdl-backend:5000', ui)
        self.assertIn('-e REACT_APP_API_SAME_ORIGIN=true', ui)
        self.assertNotIn('ApiSameOrigin', script)
        self.assertNotIn('ApiSameOrigin', subs)

    def test_the_ui_is_published_as_before(self):
        # The network adds no listener: the UI port and its bind are as before.
        script, subs = _user_data()
        self.assertIn('-p ${PublishHost}3000:3000', _docker_run(script, 'ecdl-frontend'))
        self.assertEqual(subs['PublishHost'], {'If': ['AccessSsm', '127.0.0.1:', '']})

    def test_the_api_is_on_loopback_except_behind_a_load_balancer(self):
        # The UI reaches it over the private network, the ready check and an
        # SSM port forward from the instance itself; only a load balancer that
        # routes /api/* to 5000 needs it on the instance's address.
        script, subs = _user_data()
        self.assertIn('-p ${ApiPublishHost}5000:5000', _docker_run(script, 'ecdl-backend'))
        self.assertEqual(subs['ApiPublishHost'], {'If': ['AccessLoadBalancer', '', '127.0.0.1:']})
        self.assertIn('curl -fsS -m 3 http://localhost:5000/api/auth/status', script)


def _ingress(mode):
    """The control plane's ingress rules in one AccessMode."""
    rules = _template()['Resources']['SecurityGroup']['Properties']['SecurityGroupIngress']
    pick = {'AccessLoadBalancer': mode == 'load-balancer', 'AccessCidr': mode == 'cidr'}
    while isinstance(rules, dict) and 'If' in rules:
        cond, yes, no = rules['If']
        rules = yes if pick[cond] else no
    return [] if rules == {'Ref': 'AWS::NoValue'} else rules


class IngressTest(unittest.TestCase):  # Jira story F155
    def test_cidr_mode_opens_the_ui_port_only(self):
        rules = _ingress('cidr')
        self.assertEqual([(r['FromPort'], r['ToPort']) for r in rules], [(3000, 3000)])
        self.assertEqual(rules[0]['CidrIp'], {'Ref': 'AllowedCIDR'})
        self.assertEqual(rules[0]['IpProtocol'], 'tcp')

    def test_the_load_balancer_still_reaches_both_ports(self):
        rules = _ingress('load-balancer')
        self.assertEqual(sorted(r['FromPort'] for r in rules), [3000, 5000])
        for r in rules:
            self.assertEqual(r['SourceSecurityGroupId'], {'Ref': 'LoadBalancerSecurityGroupId'})
            self.assertNotIn('CidrIp', r)

    def test_ssm_mode_opens_nothing(self):
        self.assertEqual(_ingress('ssm'), [])

    def test_no_mode_opens_5000_to_a_cidr(self):
        for mode in ('ssm', 'load-balancer', 'cidr'):
            for r in _ingress(mode):
                if r['FromPort'] <= 5000 <= r['ToPort']:
                    self.assertNotIn('CidrIp', r, mode)

    def test_the_ui_forwards_every_api_method_to_the_backend(self):
        # What lets a cidr-mode script use :3000/api/ instead of :5000.
        conf = _read(NGINX_CONF)
        block = conf[conf.index('location /api/ {'):]
        block = block[:block.index('}')]
        self.assertIn('proxy_pass $ecdl_api_upstream;', block)
        self.assertNotIn('ecdl_method_ok', block)


class EntrypointTest(unittest.TestCase):
    """docker-entrypoint.d/40-ecdl-config.sh, run with sh in a scratch dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.resolv = os.path.join(self.tmp, 'resolv.conf')
        self._resolv('nameserver 127.0.0.11\noptions ndots:0\n')

    def _resolv(self, text):
        with open(self.resolv, 'w') as handle:
            handle.write(text)

    def _run(self, **env):
        full = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'ECDL_HTML_DIR': self.tmp,
                'ECDL_API_CONF': os.path.join(self.tmp, 'ecdl-api.conf'),
                'ECDL_RESOLV_CONF': self.resolv}
        full.update(env)
        for name in ('config.js', 'ecdl-api.conf'):
            path = os.path.join(self.tmp, name)
            if os.path.exists(path):
                os.remove(path)
        res = subprocess.run(['sh', ENTRYPOINT], env=full,  # nosec B603 B607
                             capture_output=True, text=True)
        out = {}
        for name in ('config.js', 'ecdl-api.conf'):
            path = os.path.join(self.tmp, name)
            out[name] = _read(path) if os.path.exists(path) else None
        return res, out

    def test_defaults_forward_to_the_backend_container_same_origin(self):
        res, out = self._run()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out['config.js'],
                         'window.__ECDL_CONFIG__ = {"apiPort": "5000", "sameOrigin": true};\n')
        self.assertEqual(out['ecdl-api.conf'],
                         'resolver 127.0.0.11 valid=10s ipv6=off;\n'
                         'set $ecdl_api_upstream http://ecdl-backend:5000;\n')

    def test_a_custom_upstream_and_cross_origin_mode(self):
        res, out = self._run(ECDL_API_UPSTREAM='api.internal:8080',
                             REACT_APP_API_SAME_ORIGIN='false', REACT_APP_API_PORT='5006')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('"sameOrigin": false', out['config.js'])
        self.assertIn('"apiPort": "5006"', out['config.js'])
        self.assertIn('http://api.internal:8080;', out['ecdl-api.conf'])

    def test_unsafe_upstreams_are_refused_and_nothing_is_written(self):
        for bad in ('ecdl-backend', ':5000', 'host:', 'host:50x0',
                    'a;proxy_pass http://evil:1', 'a b:1', 'host:1;\nreturn 200',
                    'http://host:1', '$host:1', 'h/x:1'):
            res, out = self._run(ECDL_API_UPSTREAM=bad)
            self.assertNotEqual(res.returncode, 0, bad)
            self.assertIn('ECDL_API_UPSTREAM', res.stderr, bad)
            self.assertIsNone(out['ecdl-api.conf'], bad)
            self.assertIsNone(out['config.js'], bad)

    def test_other_settings_are_still_checked(self):
        res, _ = self._run(REACT_APP_API_PORT='50a0')
        self.assertNotEqual(res.returncode, 0)
        res, _ = self._run(REACT_APP_API_SAME_ORIGIN='yes')
        self.assertNotEqual(res.returncode, 0)

    def test_an_ipv6_nameserver_is_bracketed(self):
        self._resolv('nameserver fd00::53\n')
        res, out = self._run()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('resolver [fd00::53] ', out['ecdl-api.conf'])

    def test_no_nameserver_fails_rather_than_writing_a_broken_config(self):
        for text in ('', 'search example\n', 'nameserver 127.0.0.1;evil\n'):
            self._resolv(text)
            res, out = self._run()
            self.assertNotEqual(res.returncode, 0, text)
            self.assertIsNone(out['ecdl-api.conf'], text)


def _blocks(conf):
    """{location pattern: body text} for each location in nginx.conf."""
    found = {}
    for match in re.finditer(r'location\s+([^{]+?)\s*\{', conf):
        depth, i = 1, match.end()
        while depth:
            depth += {'{': 1, '}': -1}.get(conf[i], 0)
            i += 1
        found[match.group(1)] = conf[match.end():i - 1]
    return found


class NginxConfTest(unittest.TestCase):

    def setUp(self):
        self.conf = _read(NGINX_CONF)
        self.code = '\n'.join(line.split('#', 1)[0] for line in self.conf.splitlines())
        self.blocks = _blocks(self.code)

    def test_api_is_forwarded_with_the_browsers_host(self):
        api = self.blocks['/api/']
        self.assertIn('include /etc/nginx/ecdl-api.conf;', api)
        self.assertIn('proxy_pass $ecdl_api_upstream;', api)
        self.assertIn('proxy_set_header Host $http_host;', api)
        self.assertIn('client_max_body_size 32m;', api)

    def test_api_accepts_every_method_and_static_files_only_get_and_head(self):
        self.assertNotIn('return 405', self.blocks['/api/'])
        for loc in ('= /config.js', '/static/', '/'):
            self.assertIn('if ($ecdl_method_ok = 0)', self.blocks[loc], loc)
            self.assertIn('return 405', self.blocks[loc], loc)
        # Not at server level, where it would also refuse API writes.
        server = self.code[self.code.index('server {'):self.code.index('location')]
        self.assertNotIn('return 405', server)

    def test_every_location_sends_the_security_headers(self):
        for loc, body in self.blocks.items():
            self.assertIn('include /etc/nginx/ecdl-security-headers.conf;', body, loc)

    def test_the_image_lets_nginx_write_the_forwarding_file(self):
        dockerfile = _read(DOCKERFILE)
        self.assertIn('touch /etc/nginx/ecdl-api.conf', dockerfile)
        self.assertRegex(dockerfile, r'chown nginx:nginx [^\n]*/etc/nginx/ecdl-api\.conf')
        self.assertIn('USER nginx', dockerfile)


if __name__ == '__main__':
    unittest.main()
