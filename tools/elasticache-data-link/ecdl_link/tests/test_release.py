"""release.sh push: what it refuses, and the lock it writes (Jira stories F182, F183).

  Jira story F182  images.lock records all five images or is not written (it recorded
        three and printed a warning when any were missing), and is replaced
        in one step
  Jira story F183  push refuses a tree with uncommitted changes and a repository with
        MUTABLE tags (both used to be warnings), and turns scan-on-push on

release.sh runs from a scratch git checkout holding a copy of itself, against
fake `aws` and `docker` that log their calls. Nothing reaches AWS.
"""
import os
import re
import shutil
import subprocess  # nosec B404 - runs the repo's own script against fakes
import sys
import tempfile
import unittest

from ecdl_link.tests.test_control_plane_iam import ROOT

REGISTRY = '111122223333.dkr.ecr.us-east-2.amazonaws.com'
REPOS = ('ecdl/ecdl-sink', 'ecdl/ecdl-debezium-server', 'ecdl/ecdl-loader',
         'ecdl/ecdl-control-plane-api', 'ecdl/ecdl-control-plane-ui')

FAKE_AWS = r'''#!/bin/bash
echo "aws $*" >> "$FAKE_DIR/calls"
repo=""
prev=""
for a in "$@"; do
  [ "$prev" = "--repository-name" ] || [ "$prev" = "--repository-names" ] && repo="$a"
  prev="$a"
done
case "$*" in
  *"ecr get-login-password"*) echo pw ;;
  *"ecr describe-repositories"*)
    [ "$repo" = "${FAKE_MISSING:-}" ] && exit 254
    case "$*" in *--query*) ;; *) exit 0 ;; esac
    m=IMMUTABLE; s=True
    [ "$repo" = "${FAKE_MUTABLE:-}" ] && m=MUTABLE
    [ "$repo" = "${FAKE_NOSCAN:-}" ] && s=False
    printf '%s\t%s\n' "$m" "$s" ;;
  *"ecr describe-images"*)
    if [ "$repo" = "${FAKE_NO_DIGEST:-}" ]; then echo None; exit 0; fi
    printf 'sha256:%s\n' "$("$FAKE_PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$repo")" ;;
  *) exit 0 ;;
esac
'''

FAKE_DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$FAKE_DIR/calls"
case "$1" in
  login) cat >/dev/null; exit 0 ;;
  buildx)
    case "$2" in
      imagetools)
        [ "$3" = create ] && exit 0
        ref="${@: -1}"
        if [ "$4" = "--raw" ]; then
          echo '{"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}, {"platform": {"os": "linux", "architecture": "arm64"}}]}'
          exit 0
        fi
        case "$ref" in
          *debezium-server-base*) echo "Digest: $FAKE_DBZ_DIGEST" ;;
          *) exit 1 ;;     # _digest falls back here only when ECR had nothing
        esac ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
'''


def _git(cwd, *args):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com'] + list(args),  # nosec B603 B607
                   cwd=cwd, check=True, capture_output=True)


class ReleasePushTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(os.path.join(self.repo, 'infra'))
        shutil.copy(os.path.join(ROOT, 'release.sh'), self.repo)
        with open(os.path.join(self.repo, 'VERSION'), 'w') as fh:
            fh.write('9.9.9\n')
        _git(self.repo, 'init', '-q')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'test')
        self.bin = os.path.join(self.dir, 'bin')
        os.mkdir(self.bin)
        for name, text in (('aws', FAKE_AWS), ('docker', FAKE_DOCKER)):
            path = os.path.join(self.bin, name)
            with open(path, 'w') as fh:
                fh.write(text)
            os.chmod(path, 0o755)  # nosec B103 - test stubs in a private temp dir
        with open(os.path.join(ROOT, 'release.sh')) as fh:
            self.dbz_digest = re.search(r'DBZ_UPSTREAM="[^"@]+@(sha256:[0-9a-f]{64})"',
                                        fh.read()).group(1)
        self.lock = os.path.join(self.repo, 'infra', 'images.lock')

    def _push(self, *args, **env):
        full = {'PATH': self.bin + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
                'HOME': self.dir, 'FAKE_DIR': self.dir, 'FAKE_PYTHON': sys.executable,
                'FAKE_DBZ_DIGEST': self.dbz_digest, 'ECDL_REGISTRY': REGISTRY}
        full.update(env)
        res = subprocess.run(['bash', os.path.join(self.repo, 'release.sh')] + list(args or ['push']),  # nosec B603 B607
                             cwd=self.repo, env=full, capture_output=True, text=True)
        return res

    def _calls(self):
        path = os.path.join(self.dir, 'calls')
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def _builds(self):
        return [c for c in self._calls() if c.startswith('docker buildx build')]

    def test_a_clean_push_records_all_five_digests(self):
        res = self._push()
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.lock) as fh:
            text = fh.read()
        for key in ('sink', 'debezium', 'loader', 'control_plane_api', 'control_plane_ui'):
            self.assertRegex(text, r'(?m)^{}_digest=sha256:[0-9a-f]{{64}}$'.format(key), key)
            self.assertRegex(text, r'(?m)^{}_pinned=\S+@sha256:[0-9a-f]{{64}}$'.format(key), key)
        self.assertIn('version=9.9.9', text)
        self.assertNotIn('-dirty', text)
        self.assertIn('debezium_upstream=quay.io/debezium/server:', text)
        self.assertEqual(len(self._builds()), 5)
        self.assertFalse(os.path.exists(self.lock + '.tmp'))

    def test_a_dirty_tree_is_refused_before_anything_runs(self):
        with open(os.path.join(self.repo, 'VERSION'), 'a') as fh:
            fh.write('# local edit\n')
        res = self._push()
        self.assertEqual(res.returncode, 1)
        self.assertIn('uncommitted changes', res.stderr)
        self.assertEqual(self._calls(), [])

    def test_an_untracked_file_also_makes_the_tree_dirty(self):
        with open(os.path.join(self.repo, 'notes.txt'), 'w') as fh:
            fh.write('x')
        self.assertEqual(self._push().returncode, 1)

    def test_a_local_build_of_a_dirty_tree_is_still_allowed(self):
        with open(os.path.join(self.repo, 'notes.txt'), 'w') as fh:
            fh.write('x')
        self.assertEqual(self._push('build').returncode, 0)

    def test_a_mutable_repository_is_refused_before_building(self):
        res = self._push(FAKE_MUTABLE='ecdl/ecdl-loader')
        self.assertEqual(res.returncode, 1)
        self.assertIn('MUTABLE', res.stderr)
        self.assertIn('--repository-name ecdl/ecdl-loader --image-tag-mutability IMMUTABLE',
                      res.stderr)
        self.assertEqual(self._builds(), [])
        self.assertFalse(os.path.exists(self.lock))

    def test_scan_on_push_is_turned_on_where_it_is_off(self):
        res = self._push(FAKE_NOSCAN='ecdl/ecdl-sink')
        self.assertEqual(res.returncode, 0, res.stderr)
        scans = [c for c in self._calls() if 'put-image-scanning-configuration' in c]
        self.assertEqual(len(scans), 1, scans)
        self.assertIn('--repository-name ecdl/ecdl-sink', scans[0])
        self.assertIn('scanOnPush=true', scans[0])

    def test_a_new_repository_is_immutable_and_scanned(self):
        res = self._push(FAKE_MISSING='ecdl/ecdl-control-plane-ui')
        self.assertEqual(res.returncode, 0, res.stderr)
        created = [c for c in self._calls() if 'create-repository' in c
                   and 'ecdl/ecdl-control-plane-ui' in c]
        self.assertEqual(len(created), 1)
        self.assertIn('--image-tag-mutability IMMUTABLE', created[0])
        self.assertIn('scanOnPush=true', created[0])

    def test_a_missing_digest_fails_and_keeps_the_previous_lock(self):
        with open(self.lock, 'w') as fh:
            fh.write('previous\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'lock')     # so the tree is clean
        for repo in REPOS:
            open(os.path.join(self.dir, 'calls'), 'w').close()
            res = self._push(FAKE_NO_DIGEST=repo)
            self.assertEqual(res.returncode, 1, repo)
            self.assertIn('could not read a digest', res.stderr, repo)
            self.assertIn('{}/{}:9.9.9'.format(REGISTRY, repo), res.stderr, repo)
            with open(self.lock) as fh:
                self.assertEqual(fh.read(), 'previous\n', repo)


class LockFileTest(unittest.TestCase):

    def test_no_hand_edited_lock_is_committed(self):
        # The one in the repository described 0.1.0 from an uncommitted tree,
        # with a sink line added by hand (Jira story F182). push writes it; commit that.
        path = os.path.join(ROOT, 'infra', 'images.lock')
        if not os.path.exists(path):
            return
        with open(path) as fh:
            text = fh.read()
        with open(os.path.join(ROOT, 'VERSION')) as fh:
            version = fh.read().strip()
        self.assertIn('version={}\n'.format(version), text)
        self.assertNotRegex(text, r'(?m)^commit=.*-dirty$')
        self.assertEqual(len(re.findall(r'(?m)^\w+_digest=sha256:[0-9a-f]{64}$', text)), 5)


class ExecutableBitTest(unittest.TestCase):
    """The release box runs ./release.sh from a git archive, so the mode
    recorded in git is what it gets. A merge once dropped it to 100644."""
    SCRIPTS = ('release.sh', 'ecdl-cli/ecdl', 'ecdl-cli/ecdl-ui',
               'data-plane/debezium-server/entrypoint.sh')
    def test_scripts_are_executable_in_git(self):
        run = subprocess.run(['git', 'ls-files', '-s', '--'] + list(self.SCRIPTS),  # nosec B603 B607
                             cwd=ROOT, capture_output=True, text=True, check=False)
        modes = {line.split('\t', 1)[1]: line.split()[0] for line in run.stdout.splitlines()}
        if run.returncode != 0 or not modes:
            # A source export (git archive, a release tarball): no index to read.
            self.skipTest('not a git checkout')
        for script in self.SCRIPTS:
            self.assertEqual(modes.get(script), '100755', script)
if __name__ == '__main__':
    unittest.main()
