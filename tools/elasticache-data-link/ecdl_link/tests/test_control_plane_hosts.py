"""The control plane sets the API's Host allowlist in every AccessMode (Jira story F146).

backend/auth.py answers only requests whose Host header is on
ECDL_ALLOWED_HOSTS, and loopback only when it is unset. That stops DNS
rebinding, but only if each way of running the API sets a list that matches how
operators open it. It used to be set in ssm mode only, so docker compose and
cidr mode answered any Host. These tests run the user-data snippet that builds
the list, with a fake metadata service, for each mode.
"""
import os
import re
import shutil
import subprocess  # nosec B404 - runs bash on a fixed snippet from the template
import tempfile
import unittest

import yaml

from ecdl_link.tests.test_control_plane_iam import _template, ROOT

COMPOSE = os.path.join(ROOT, 'docker-compose.yml')

# What the fake metadata service returns. public-* are absent (curl -f fails)
# unless given.
PRIVATE = {'local-ipv4': '10.0.1.23', 'local-hostname': 'ip-10-0-1-23.ec2.internal'}
PUBLIC = {'public-ipv4': '203.0.113.9',
          'public-hostname': 'ec2-203-0-113-9.us-east-2.compute.amazonaws.com'}


def _user_data():
    """(script text, substitution map) of the instance's user data."""
    sub = _template()['Resources']['Instance']['Properties']['UserData']['Fn::Base64']['Sub']
    return sub[0], sub[1]


def _pick(value, mode):
    """Resolve the !If tree in BackendAccessEnv for one AccessMode."""
    while isinstance(value, dict) and 'If' in value:
        cond, yes, no = value['If']
        value = yes if {'AccessSsm': 'ssm', 'AccessLoadBalancer': 'load-balancer',
                        'AccessCidr': 'cidr'}[cond] == mode else no
    return value


def _run(mode, ui_host_names='', metadata=None):
    """The ECDL_ALLOWED_HOSTS value the backend container would be given."""
    script, subs = _user_data()
    start = script.index('IMDS=http://169.254.169.254')
    end = script.index('docker run -d --name ecdl-backend')
    snippet = script[start:end].replace('${UiHostNames}', ui_host_names)
    env_args = _pick(subs['BackendAccessEnv'], mode)
    tmp = tempfile.mkdtemp()
    try:
        md = dict(metadata or {})
        # A fake curl: the token PUT returns a token; a meta-data GET returns the
        # item or fails like curl -f on a 404.
        lines = ['#!/bin/bash',
                 'for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done',
                 'case "$url" in', '  */api/token) echo tok ;;']
        for key, val in md.items():
            lines.append('  */meta-data/{}) echo {} ;;'.format(key, val))
        lines += ['  *) exit 22 ;;', 'esac']
        curl = os.path.join(tmp, 'curl')
        with open(curl, 'w') as handle:
            handle.write('\n'.join(lines) + '\n')
        os.chmod(curl, 0o755)  # nosec B103 - a test stub in a private temp dir
        program = 'set -euo pipefail\n{}\nprintf "%s\\n" {}\n'.format(snippet, env_args)
        out = subprocess.run(  # nosec B603 B607 - fixed program, test only
            ['bash', '-c', program], capture_output=True, text=True,
            env={'PATH': tmp + os.pathsep + os.environ.get('PATH', '')}, check=True)
    finally:
        shutil.rmtree(tmp)
    args = out.stdout.split()
    hosts = [a.split('=', 1)[1] for a in args if a.startswith('ECDL_ALLOWED_HOSTS=')]
    origins = [a for a in args if a.startswith('ECDL_ALLOWED_ORIGINS=')]
    return hosts, origins


class AllowedHostsTest(unittest.TestCase):

    def test_ssm_mode_is_loopback_on_any_port(self):
        # ecdl-ui --ui-port / --api-port choose the local ports (Jira story F144), and the
        # UI forwards /api/* itself (Jira story F155), so the Host is localhost:<any>.
        hosts, origins = _run('ssm', metadata=PRIVATE)
        self.assertEqual(hosts, ['localhost,127.0.0.1,[::1]'])
        self.assertEqual(origins, [])

    def test_cidr_mode_lists_the_private_address_and_name(self):
        hosts, origins = _run('cidr', metadata=PRIVATE)
        self.assertEqual(hosts, ['localhost,127.0.0.1,[::1],10.0.1.23,'
                                 'ip-10-0-1-23.ec2.internal'])
        self.assertEqual(origins, [])

    def test_cidr_mode_adds_the_public_address_when_there_is_one(self):
        hosts, _ = _run('cidr', metadata=dict(PRIVATE, **PUBLIC))
        listed = hosts[0].split(',')
        self.assertIn('203.0.113.9', listed)
        self.assertIn(PUBLIC['public-hostname'], listed)

    def test_cidr_mode_adds_operator_names(self):
        hosts, _ = _run('cidr', 'ecdl.example.com,ui.example.com:8080', PRIVATE)
        self.assertTrue(hosts[0].endswith(',ecdl.example.com,ui.example.com:8080'))

    def test_load_balancer_mode_lists_its_domain_and_keeps_same_origin(self):
        hosts, origins = _run('load-balancer', 'ecdl.example.com', PRIVATE)
        listed = hosts[0].split(',')
        # The domain for users, the private address for the target group's
        # health check (the load balancer sends the target's IP as Host).
        self.assertIn('ecdl.example.com', listed)
        self.assertIn('10.0.1.23', listed)
        self.assertEqual(origins, ['ECDL_ALLOWED_ORIGINS='])

    def test_a_metadata_failure_still_leaves_a_closed_list(self):
        # No addresses come back: the list is loopback, never empty and never any.
        hosts, _ = _run('cidr', metadata={})
        self.assertEqual(hosts, ['localhost,127.0.0.1,[::1]'])

    def test_every_mode_sets_the_variable(self):
        for mode in ('ssm', 'cidr', 'load-balancer'):
            hosts, _ = _run(mode, 'x.example', PRIVATE)
            self.assertEqual(len(hosts), 1, mode)
            self.assertTrue(hosts[0], mode)

    def test_the_whole_script_parses_in_every_mode(self):
        script, subs = _user_data()
        shell = script.split('filename="userdata.txt"', 1)[1].split('--//--')[0]
        for mode in ('ssm', 'cidr', 'load-balancer'):
            text = shell.replace('${BackendAccessEnv}',
                                 _pick(subs['BackendAccessEnv'], mode))
            text = re.sub(r'\$\{[A-Za-z0-9:.]+\}', 'X', text)
            res = subprocess.run(['bash', '-n'], input=text,  # nosec B603 B607
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, '{}: {}'.format(mode, res.stderr))

    def test_the_readiness_check_uses_an_allowed_host(self):
        # cfn-signal depends on this curl getting 200, not the host check's 403.
        script, _ = _user_data()
        self.assertIn('http://localhost:5000/api/auth/status', script)


class UiHostNamesParameterTest(unittest.TestCase):

    def setUp(self):
        self.template = _template()
        self.param = self.template['Parameters']['UiHostNames']
        self.pattern = re.compile(self.param['AllowedPattern'])

    def test_accepts_names_with_and_without_ports(self):
        for value in ('', 'ecdl.example.com', 'a.example,b.example:8443',
                      'internal-ecdl-123.us-east-2.elb.amazonaws.com'):
            self.assertTrue(self.pattern.match(value), value)

    def test_rejects_anything_that_could_reach_the_shell(self):
        # The value is pasted into the user-data script.
        for value in ("a'b", 'a;b', 'a b', '$(id)', 'a`b`', 'a,', ',a', 'a:', 'a"b'):
            self.assertIsNone(self.pattern.match(value), value)

    def test_load_balancer_mode_requires_it(self):
        rule = self.template['Rules']['LoadBalancerModeNeedsUiHostNames']
        self.assertEqual(rule['RuleCondition'],
                         {'Equals': [{'Ref': 'AccessMode'}, 'load-balancer']})
        self.assertEqual(rule['Assertions'][0]['Assert'],
                         {'Not': [{'Equals': [{'Ref': 'UiHostNames'}, '']}]})


class ComposeTest(unittest.TestCase):

    def test_compose_sets_loopback_hosts_explicitly(self):
        with open(COMPOSE) as handle:
            compose = yaml.safe_load(handle)
        env = compose['services']['backend']['environment']
        self.assertIn('ECDL_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]', env)


if __name__ == '__main__':
    unittest.main()
