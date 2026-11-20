"""ecdl-cli/ecdl-ui: port conflicts, dead tunnels and keep-alive (Jira stories F144, F145).

The script is run with /bin/bash (3.2 on macOS, the oldest it must support)
against a fake `aws` whose `ssm start-session` serves HTTP on the requested
local port, so the tunnels are real listeners that a test can close.
"""
import os
import shutil
import socket
import subprocess  # nosec B404 - runs the repo's own script against fakes
import sys
import tempfile
import time
import unittest

from ecdl_link.tests.test_control_plane_iam import ROOT

SCRIPT = os.path.join(ROOT, 'ecdl-cli', 'ecdl-ui')
BASH = '/bin/bash' if os.path.exists('/bin/bash') else shutil.which('bash')

# `aws`: answers describe-stacks, and for start-session becomes the fake tunnel
# (exec), so its PID is the listener's.
FAKE_AWS = r'''#!/bin/bash
echo "$*" >> "$FAKE_DIR/calls"
case "$*" in
  *"cloudformation describe-stacks"*)
    case "$*" in
      *InstanceId*) echo "${FAKE_INSTANCE-i-0123456789abcdef0}" ;;
      *AccessMode*) echo "${FAKE_MODE-ssm}" ;;
    esac ;;
  *"ssm start-session"*)
    for a in "$@"; do case "$a" in portNumber=*) params="$a" ;; esac; done
    local_port="${params##*localPortNumber=}"
    exec "$FAKE_PYTHON" "$FAKE_DIR/tunnel.py" "$local_port" ;;
esac
'''

# The tunnel: an HTTP server on 127.0.0.1:<port> that logs request paths.
FAKE_TUNNEL = r'''
import http.server, os, sys, threading, time
port = int(sys.argv[1])
log = os.path.join(os.environ['FAKE_DIR'], 'requests')
if os.environ.get('FAKE_FAIL_START'):
    print('An error occurred (TargetNotConnected) when calling the StartSession '
          'operation: i-0123 is not connected.', flush=True)
    sys.exit(254)
print('Starting session with SessionId: test-0123', flush=True)
if str(port) == os.environ.get('FAKE_NEVER_LISTEN_PORT'):
    time.sleep(3600)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(log, 'a') as fh:
            fh.write('{} {} {}\n'.format(port, self.path, self.headers.get('Host')))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(('127.0.0.1', port), H)
print('Waiting for connections...', flush=True)
if str(port) == os.environ.get('FAKE_DIE_PORT'):
    def die():
        time.sleep(float(os.environ.get('FAKE_DIE_AFTER', '2')))
        print('Session test-0123 closed: idle timeout', flush=True)
        os._exit(0)
    threading.Thread(target=die, daemon=True).start()
srv.serve_forever()
'''

FAKE_LSOF = r'''#!/bin/bash
[ -n "${FAKE_LSOF_CMD:-}" ] || exit 1
echo "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME"
echo "$FAKE_LSOF_CMD 4242 user 9u IPv4 0x0 0t0 TCP *:5000 (LISTEN)"
'''

FAKE_UNAME = '#!/bin/bash\necho "${FAKE_UNAME:-Linux}"\n'


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _listening(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


class EcdlUiTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, 'bin')
        os.mkdir(self.bin)
        for name, text in (('aws', FAKE_AWS), ('lsof', FAKE_LSOF), ('uname', FAKE_UNAME),
                           ('session-manager-plugin', '#!/bin/bash\nexit 0\n')):
            path = os.path.join(self.bin, name)
            with open(path, 'w') as fh:
                fh.write(text)
            os.chmod(path, 0o755)  # nosec B103 - test stubs in a private temp dir
        with open(os.path.join(self.dir, 'tunnel.py'), 'w') as fh:
            fh.write(FAKE_TUNNEL)
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def _env(self, **extra):
        env = {'PATH': self.bin + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
               'FAKE_DIR': self.dir, 'FAKE_PYTHON': sys.executable,
               'TMPDIR': self.dir, 'ECDL_UI_POLL_SECONDS': '1',
               'ECDL_UI_OPEN_TIMEOUT': '15'}
        env.update(extra)
        return env

    def _start(self, *args, **env):
        out = open(os.path.join(self.dir, 'out'), 'w')
        err = open(os.path.join(self.dir, 'err'), 'w')
        proc = subprocess.Popen([BASH, SCRIPT, 'cp-stack'] + list(args),  # nosec B603
                                stdout=out, stderr=err, env=self._env(**env))
        out.close()
        err.close()
        self.procs.append(proc)
        return proc

    def _read(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return ''
        with open(path) as fh:
            return fh.read()

    def _wait_for(self, text, proc, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if text in self._read('out'):
                return
            if proc.poll() is not None:
                self.fail('exited {} before "{}":\n{}{}'.format(
                    proc.returncode, text, self._read('out'), self._read('err')))
            time.sleep(0.1)
        self.fail('no "{}" after {}s:\n{}'.format(text, timeout, self._read('err')))

    def _run(self, *args, **env):
        proc = self._start(*args, **env)
        proc.wait(60)
        return proc.returncode, self._read('out'), self._read('err')

    def _sessions(self):
        return [line for line in self._read('calls').splitlines() if 'start-session' in line]

    # ── the default: one tunnel, the UI ────────────────────────────────────

    def test_default_forwards_only_the_ui_and_stays_up(self):
        port = _free_port()
        proc = self._start('--ui-port', str(port))
        self._wait_for('Open http://localhost:{}'.format(port), proc)
        sessions = self._sessions()
        self.assertEqual(len(sessions), 1, sessions)
        self.assertIn('portNumber=3000,localPortNumber={}'.format(port), sessions[0])
        time.sleep(2)
        self.assertIsNone(proc.poll(), 'exited while the tunnel was up')
        proc.terminate()
        proc.wait(10)
        # Ctrl-C/TERM closes the tunnel.
        time.sleep(0.5)
        self.assertFalse(_listening(port))

    def test_api_port_adds_the_api_tunnel(self):
        ui, api = _free_port(), _free_port()
        proc = self._start('--ui-port', str(ui), '--api-port', str(api))
        self._wait_for('API for the ecdl CLI: http://localhost:{}'.format(api), proc)
        sessions = ' | '.join(self._sessions())     # started in parallel: any order
        self.assertEqual(len(self._sessions()), 2)
        self.assertIn('portNumber=3000,localPortNumber={}'.format(ui), sessions)
        self.assertIn('portNumber=5000,localPortNumber={}'.format(api), sessions)

    # ── Jira story F145: a tunnel that closes is reported, and the script exits 1 ─────

    def test_a_tunnel_that_closes_ends_the_script_with_the_reason(self):
        ui, api = _free_port(), _free_port()
        proc = self._start('--ui-port', str(ui), '--api-port', str(api),
                           FAKE_DIE_PORT=str(api), FAKE_DIE_AFTER='2')
        self._wait_for('Open http://', proc)
        proc.wait(20)
        err = self._read('err')
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn('the API tunnel (local port {}) closed'.format(api), err)
        self.assertIn('idle timeout', err)          # what the session last said
        time.sleep(0.5)
        self.assertFalse(_listening(ui), 'the other tunnel was left open')

    def test_a_session_that_fails_to_start_is_reported(self):
        code, _, err = self._run('--ui-port', str(_free_port()), FAKE_FAIL_START='1')
        self.assertEqual(code, 1)
        self.assertIn('closed while opening', err)
        self.assertIn('TargetNotConnected', err)

    def test_a_tunnel_that_never_listens_times_out(self):
        port = _free_port()
        code, _, err = self._run('--ui-port', str(port), FAKE_NEVER_LISTEN_PORT=str(port),
                                 ECDL_UI_OPEN_TIMEOUT='3')
        self.assertEqual(code, 1)
        self.assertIn('did not open local port {} within 3s'.format(port), err)

    # ── keep-alive ─────────────────────────────────────────────────────────

    def test_keepalive_sends_requests_through_each_tunnel(self):
        ui, api = _free_port(), _free_port()
        proc = self._start('--ui-port', str(ui), '--api-port', str(api), '--keepalive', '1')
        self._wait_for('Open http://', proc)
        time.sleep(4)
        lines = self._read('requests').splitlines()
        self.assertGreaterEqual(sum(1 for l in lines if l.startswith('{} /config.js'.format(ui))), 2)
        self.assertGreaterEqual(
            sum(1 for l in lines if l.startswith('{} /api/auth/status'.format(api))), 2)

    def test_keepalive_zero_sends_nothing(self):
        proc = self._start('--ui-port', str(_free_port()), '--keepalive', '0')
        self._wait_for('Open http://', proc)
        time.sleep(3)
        self.assertEqual(self._read('requests'), '')

    # ── Jira story F144: a port in use, and AirPlay ───────────────────────────────────

    def _busy(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_a_busy_port_names_the_process_and_the_option(self):
        port = self._busy()
        code, _, err = self._run('--ui-port', str(port), FAKE_LSOF_CMD='node')
        self.assertEqual(code, 1)
        self.assertIn('local port {} is already in use (by node)'.format(port), err)
        self.assertIn('--ui-port N', err)
        self.assertNotIn('AirPlay', err)
        self.assertEqual(self._sessions(), [], 'started a session anyway')

    def test_airplay_is_named_when_control_center_holds_the_port(self):
        port = self._busy()
        code, _, err = self._run('--ui-port', str(_free_port()), '--api-port', str(port),
                                 FAKE_LSOF_CMD='ControlCe')
        self.assertEqual(code, 1)
        self.assertIn('AirPlay Receiver', err)
        self.assertIn('--api-port N', err)

    def test_airplay_is_suggested_for_5000_on_macos_when_lsof_says_nothing(self):
        holder = None
        if not _listening(5000):
            holder = socket.socket()
            try:
                holder.bind(('127.0.0.1', 5000))
            except OSError:
                holder.close()
                self.skipTest('port 5000 is taken but not on 127.0.0.1')
            holder.listen(1)
            self.addCleanup(holder.close)
        code, _, err = self._run('--ui-port', str(_free_port()), '--api-port', '5000',
                                 FAKE_UNAME='Darwin')
        self.assertEqual(code, 1)
        self.assertIn('AirPlay Receiver', err)
        code, _, err = self._run('--ui-port', str(_free_port()), '--api-port', '5000',
                                 FAKE_UNAME='Linux')
        self.assertNotIn('AirPlay', err)

    # ── arguments ──────────────────────────────────────────────────────────

    def test_bad_arguments_are_usage_errors(self):
        for args in (['--ui-port', 'abc'], ['--ui-port', '0'], ['--api-port', '70000'],
                     ['--ui-port', '4000', '--api-port', '4000'], ['--keepalive', '-1'],
                     ['--keepalive'], ['--bogus']):
            code, _, err = self._run(*args)
            self.assertEqual(code, 2, (args, err))
            self.assertIn('usage:', err, args)
        self.assertEqual(self._sessions(), [])

    def test_a_stack_in_another_mode_is_refused(self):
        code, _, err = self._run(FAKE_MODE='cidr')
        self.assertEqual(code, 1)
        self.assertIn('AccessMode=cidr', err)


if __name__ == '__main__':
    unittest.main()
