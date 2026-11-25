"""SIGTERM stops the loader and every worker at once (load handoff H1).

Runs a real process tree: a parent with the loader's stop handler and a pool of
workers that would otherwise write for a minute, then sends SIGTERM to the
parent only, as ECS does to PID 1."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOADER = os.path.dirname(HERE)

SCRIPT = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, {loader!r})
    from concurrent.futures import ProcessPoolExecutor

    def work(i):
        with open(os.path.join({out!r}, 'w%d' % os.getpid()), 'w') as f:
            f.write(str(os.getpid()))
        time.sleep(60)
        return i

    if __name__ == '__main__':
        {install}
        with ProcessPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(work, i) for i in range(3)]
            print('ready', flush=True)
            for f in futs:
                f.result()
''')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class StopSignalTest(unittest.TestCase):
    def run_tree(self, install):
        out = tempfile.mkdtemp()
        path = os.path.join(out, 'tree.py')
        with open(path, 'w') as f:
            f.write(SCRIPT.format(loader=LOADER, out=out, install=install))
        # executable= runs this same interpreter; 'python3' is only its argv[0].
        proc = subprocess.Popen(['python3', '-u', path], executable=sys.executable,  # nosec B603 - the test's own script
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), 'ready')
        deadline = time.time() + 20
        while time.time() < deadline:
            pids = [int(open(os.path.join(out, n)).read() or 0)
                    for n in os.listdir(out) if n.startswith('w')]
            if len(pids) == 3 and all(pids):
                break
            time.sleep(0.1)
        self.assertEqual(len(pids), 3, 'workers did not start')
        self.addCleanup(lambda: [os.kill(p, signal.SIGKILL) for p in pids if alive(p)])
        start = time.time()
        proc.send_signal(signal.SIGTERM)
        return proc, pids, start

    def test_sigterm_stops_parent_and_workers_quickly(self):
        proc, pids, start = self.run_tree(
            'import bulk_loader; bulk_loader.install_stop_handler()')
        code = proc.wait(timeout=15)
        self.assertEqual(code, 143)
        time.sleep(0.5)
        self.assertEqual([p for p in pids if alive(p)], [], 'workers kept running')
        self.assertLess(time.time() - start, 10)


class MainInstallsHandlerTest(unittest.TestCase):
    def test_main_installs_the_handler_before_anything_else(self):
        sys.path.insert(0, LOADER)
        import bulk_loader
        calls = []
        saved_install, saved_argv = bulk_loader.install_stop_handler, sys.argv
        bulk_loader.install_stop_handler = lambda: calls.append('installed')
        sys.argv = ['bulk_loader.py', '--help']
        try:
            with self.assertRaises(SystemExit):
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    bulk_loader.main()
        finally:
            bulk_loader.install_stop_handler, sys.argv = saved_install, saved_argv
        self.assertEqual(calls, ['installed'])


if __name__ == '__main__':
    unittest.main()
