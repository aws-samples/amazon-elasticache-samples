"""Jira stories F179 and F100: the control plane's user data, run with fake commands.

Jira story F179. An update with an image that does not start must fail the stack. The
instance's CreationPolicy gates only its creation, so each release has its own
wait condition (named for its ImageTag), which the user data signals on every
boot with the API health check's result.

Jira story F100. Container output goes to the control plane's log group with the awslogs
driver when CloudWatch Logs is reachable, and stays on the instance (with a
line saying so) when it is not, rather than stopping the control plane.

The whole user-data script runs under bash with fakes on PATH for dnf,
systemctl, aws, docker, curl and sleep, and a fake cfn-signal that records
its arguments, in the way test_control_plane_hosts runs its snippet.
"""
import os
import re
import shutil
import subprocess  # nosec B404 - runs bash on the template's own user data
import tempfile
import unittest


from ecdl_link.safe_yaml import load_cfn
from ecdl_link.tests.test_control_plane_iam import ROOT, _template

# A presigned S3 URL, with the characters a shell could mangle.
HANDLE = ('https://cloudformation-waitcondition-us-east-2.s3.us-east-2.amazonaws.com/'
          'arn%3Aaws%3Acloudformation%3Aus-east-2%3A111122223333%3Astack/cp/abc/ReadyHandle'
          '?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20250101T000000Z&X-Amz-Signature=0f')

SUBS = {
    'AWS::StackName': 'cp', 'AWS::Region': 'us-east-2', 'AWS::AccountId': '111122223333',
    'AWS::URLSuffix': 'amazonaws.com', 'UiHostNames': '', 'TaskRoleBoundary': 'arn:b',
    'PipelineSecretList': '', 'DeployRole.Arn': 'arn:r', 'TemplateBucket': 'bucket',
    'ImageTag': '0.0.1', 'BackendAccessEnv': '-e ECDL_ALLOWED_HOSTS=localhost:5000',
    'ApiSameOrigin': 'false', 'PublishHost': '127.0.0.1:', 'ApiPublishHost': '127.0.0.1:',
    'BackendRef': '111122223333.dkr.ecr.us-east-2.amazonaws.com/ecdl/ecdl-control-plane-api:0.0.1',
    'FrontendRef': '111122223333.dkr.ecr.us-east-2.amazonaws.com/ecdl/ecdl-control-plane-ui:0.0.1',
    'ControlPlaneLogGroup': '/ecdl/control-plane/cp', 'ReadyHandle': HANDLE,
}


def _script():
    sub = _template()['Resources']['Instance']['Properties']['UserData']['Fn::Base64']['Sub']
    text = sub[0]
    body = text[text.index('#!/bin/bash'):text.index('--//--')]
    names = set(re.findall(r'\$\{([A-Za-z0-9:.]+)\}', body))
    missing = names - set(SUBS)
    if missing:
        raise AssertionError('user data uses unknown substitutions: {}'.format(sorted(missing)))
    for key, value in SUBS.items():
        body = body.replace('${%s}' % key, value)
    return body


def _fake(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, 'w') as handle:
        handle.write('#!/bin/bash\n' + body + '\n')
    os.chmod(path, 0o755)  # nosec B103 - a test stub in a private temp dir


def run(api_up=True, logs_reachable=True, pull_ok=True):
    """Run the user data; returns (exit code, cfn-signal calls, docker run lines, stderr)."""
    tmp = tempfile.mkdtemp()
    try:
        bin_dir = os.path.join(tmp, 'bin')
        os.mkdir(bin_dir)
        log = os.path.join(tmp, 'calls')
        for name in ('dnf', 'systemctl', 'sleep', 'chown'):
            _fake(bin_dir, name, 'exit 0')
        _fake(bin_dir, 'aws', '''
case "$1 $2" in
  "ecr get-login-password") echo token ;;
  "logs describe-log-streams") {} ;;
  *) exit 0 ;;
esac'''.format('exit 0' if logs_reachable else 'exit 255'))
        # `docker login --password-stdin` reads the token piped to it; a fake
        # that does not lets `aws ... | docker login` fail with SIGPIPE (141)
        # under pipefail when docker exits first.
        _fake(bin_dir, 'docker', '''
echo "docker $*" >> {log}
case " $* " in *" --password-stdin "*) cat > /dev/null ;; esac
case "$1" in
  pull) {pull} ;;
  logs) echo "backend crashed" ;;
esac
exit 0'''.format(log=log, pull='exit 0' if pull_ok else 'echo "manifest unknown" >&2; exit 1'))
        _fake(bin_dir, 'curl', '''
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
case "$url" in
  */api/token) echo tok ;;
  */meta-data/local-ipv4) echo 10.0.1.23 ;;
  */api/auth/status) {} ;;
  *) exit 22 ;;
esac'''.format('exit 0' if api_up else 'exit 7'))
        signal = os.path.join(bin_dir, 'cfn-signal')
        # One line per call; the arguments separated by a tab.
        _fake(bin_dir, 'cfn-signal', 'printf "%s\\t" "$@" >> {}; echo >> {}'.format(
            os.path.join(tmp, 'signals'), os.path.join(tmp, 'signals')))
        script = (_script().replace('/opt/aws/bin/cfn-signal', signal)
                  .replace('/var/lib/ecdl/data', os.path.join(tmp, 'data')))
        path = os.path.join(tmp, 'user-data.sh')
        with open(path, 'w') as handle:
            handle.write(script)
        proc = subprocess.run(  # nosec B603 B607 - the template's script, test only
            ['bash', path], capture_output=True, text=True,
            env={'PATH': bin_dir + os.pathsep + os.environ.get('PATH', ''), 'HOME': tmp})
        signals = []
        if os.path.exists(os.path.join(tmp, 'signals')):
            with open(os.path.join(tmp, 'signals')) as handle:
                signals = [line.rstrip('\t\n').split('\t') for line in handle if line.strip()]
        runs = []
        if os.path.exists(log):
            with open(log) as handle:
                runs = [line.strip() for line in handle if line.startswith('docker run')]
        return proc.returncode, signals, runs, proc.stderr
    finally:
        shutil.rmtree(tmp)


def _handle_signals(signals):
    return [s for s in signals if s[-1] == HANDLE]


def _exit_code(call):
    return call[call.index('-e') + 1]


class WaitConditionSignalTest(unittest.TestCase):
    def test_a_healthy_start_signals_success_to_the_handle_and_the_instance(self):
        rc, signals, _, err = run()
        self.assertEqual(rc, 0, err)
        handle = _handle_signals(signals)
        self.assertEqual([_exit_code(s) for s in handle], ['0'])
        instance = [s for s in signals if '--resource' in s]
        self.assertEqual([_exit_code(s) for s in instance], ['0'])
        self.assertEqual(instance[0][instance[0].index('--resource') + 1], 'Instance')

    def test_an_api_that_never_answers_fails_the_wait_condition(self):
        rc, signals, _, _ = run(api_up=False)
        self.assertNotEqual(rc, 0)
        codes = [_exit_code(s) for s in _handle_signals(signals)]
        self.assertTrue(codes)
        self.assertNotIn('0', codes)

    def test_an_image_that_cannot_be_pulled_fails_the_wait_condition_at_once(self):
        rc, signals, runs, _ = run(pull_ok=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual([_exit_code(s) for s in _handle_signals(signals)], ['1'])
        self.assertEqual(runs, [])                 # stopped before starting anything


class AwsLogsTest(unittest.TestCase):
    def test_containers_log_to_the_group_when_logs_is_reachable(self):
        rc, _, runs, err = run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(runs), 2)
        for line, stream in zip(runs, ('backend', 'frontend')):
            self.assertIn('--log-driver awslogs', line)
            self.assertIn('--log-opt awslogs-group=/ecdl/control-plane/cp', line)
            self.assertIn('--log-opt awslogs-region=us-east-2', line)
            self.assertIn('--log-opt mode=non-blocking', line)
            self.assertIn('--log-opt awslogs-stream=' + stream, line)

    def test_an_unreachable_logs_api_keeps_the_default_driver_and_says_so(self):
        rc, signals, runs, err = run(logs_reachable=False)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(runs), 2)
        for line in runs:
            self.assertNotIn('--log-driver', line)
        self.assertIn('CloudWatch Logs not reachable', err)
        self.assertEqual([_exit_code(s) for s in _handle_signals(signals)], ['0'])


def _ready_names(text):
    return sorted(set(re.findall(r'\bReady(?:Handle)?V[A-Za-z0-9]+\b', text)))


class ReleaseNamesTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, 'infra', 'ecdl-control-plane.yaml')
        with open(self.path) as handle:
            self.text = handle.read()

    def test_the_wait_condition_is_named_for_the_pinned_image_tag(self):
        t = load_cfn(self.text)
        tag = re.sub(r'[^A-Za-z0-9]', 'x', str(t['Parameters']['ImageTag']['Default']))
        res = t['Resources']
        handle, wait = 'ReadyHandleV' + tag, 'ReadyV' + tag
        self.assertEqual(res[handle]['Type'], 'AWS::CloudFormation::WaitConditionHandle')
        self.assertEqual(res[wait]['Type'], 'AWS::CloudFormation::WaitCondition')
        self.assertEqual(res[wait]['Properties']['Handle'], {'Ref': handle})
        self.assertEqual(res[wait]['Properties']['Count'], 1)
        self.assertEqual(res[wait]['DependsOn'], 'Instance')
        self.assertNotIn('CreationPolicy', res[wait])
        sub = res['Instance']['Properties']['UserData']['Fn::Base64']['Sub']
        self.assertEqual(sub[1]['ReadyHandle'], {'Ref': handle})
        self.assertEqual(_ready_names(self.text), [handle, wait])

    def test_release_pin_renames_them(self):
        with open(os.path.join(ROOT, 'release.sh')) as handle:
            release = handle.read()
        program = release[release.index("<<'PY'\n") + 7:release.index('\nPY\n')]
        tmp = tempfile.mkdtemp()
        try:
            copy = os.path.join(tmp, 'cp.yaml')
            shutil.copyfile(self.path, copy)
            subprocess.run(['python3', '-', copy, '1.2.30'], input=program, text=True,  # nosec B603 B607
                           check=True, capture_output=True)
            with open(copy) as handle:
                pinned = handle.read()
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(_ready_names(pinned), ['ReadyHandleV1x2x30', 'ReadyV1x2x30'])
        t = load_cfn(pinned)
        self.assertEqual(t['Parameters']['ImageTag']['Default'], '1.2.30')
        self.assertEqual(t['Resources']['ReadyV1x2x30']['Properties']['Handle'],
                         {'Ref': 'ReadyHandleV1x2x30'})


if __name__ == '__main__':
    unittest.main()
