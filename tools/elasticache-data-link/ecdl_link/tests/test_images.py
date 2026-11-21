"""Container images: reproducible inputs and no root (Jira stories F181, F184, F185).

  Jira story F181  every FROM (and the JDK build argument) names a digest, and the Python
        images install a hashed lock file that matches requirements.txt
  Jira story F184  the API images run as a non-root uid that the control plane's data
        directory is given to, and the compose build context leaves data/ out
  Jira story F185  the API's dependencies are pinned at versions without the known CVEs
"""
import os
import re
import unittest

from ecdl_link.tests.test_control_plane_iam import _template, ROOT

DIGEST = re.compile(r'@sha256:[0-9a-f]{64}$')

DOCKERFILES = (
    'backend/Dockerfile', 'backend/Dockerfile.release', 'tools/go-sink/Dockerfile',
    'data-plane/bulk-loader/Dockerfile', 'data-plane/debezium-server/Dockerfile',
    'frontend/Dockerfile', 'frontend/Dockerfile.prod',
)

# (requirements.txt, requirements.lock, Dockerfiles that install the lock)
PYTHON_IMAGES = (
    ('backend/requirements.txt', 'backend/requirements.lock',
     ('backend/Dockerfile', 'backend/Dockerfile.release')),
    ('data-plane/bulk-loader/requirements.txt', 'data-plane/bulk-loader/requirements.lock',
     ('data-plane/bulk-loader/Dockerfile',)),
)


def _read(rel):
    with open(os.path.join(ROOT, rel)) as handle:
        return handle.read()


def _instructions(text):
    """Dockerfile instructions with continuation lines joined, comments dropped."""
    out, current = [], ''
    for line in text.splitlines():
        if line.lstrip().startswith('#') and not current:
            continue
        if line.rstrip().endswith('\\'):
            current += line.rstrip()[:-1] + ' '
            continue
        current += line
        if current.strip():
            out.append(current.strip())
        current = ''
    return out


def _pins(text):
    """{name: version} of `name==version` lines in a requirements file."""
    found = {}
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip().rstrip('\\').strip()
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;]+)', line)
        if match:
            found[match.group(1).lower().replace('_', '-')] = match.group(2)
    return found


def _version(text):
    return tuple(int(p) for p in re.findall(r'\d+', text)[:3])


class BaseImagesTest(unittest.TestCase):
    """Jira story F181."""

    def test_every_from_is_pinned_by_digest(self):
        for rel in DOCKERFILES:
            for ins in _instructions(_read(rel)):
                if not ins.upper().startswith('FROM '):
                    continue
                ref = [w for w in ins.split()[1:] if not w.startswith('--')][0]
                if ref.startswith('${'):
                    continue            # a build argument, checked below
                self.assertRegex(ref, DIGEST, '{}: {}'.format(rel, ins))

    def test_image_build_arguments_default_to_a_digest(self):
        text = _read('data-plane/debezium-server/Dockerfile')
        jdk = re.search(r'^ARG JDK_IMAGE=(\S+)$', text, re.M)
        self.assertRegex(jdk.group(1), DIGEST)
        jre = re.search(r'^ARG JRE_IMAGE=(\S+)$', text, re.M)
        self.assertRegex(jre.group(1), DIGEST)
        # DEBEZIUM_BASE has no default on purpose; release.sh passes a digest.
        self.assertRegex(text, r'(?m)^ARG DEBEZIUM_BASE$')
        self.assertRegex(_read('release.sh'), r'DBZ_UPSTREAM="[^"]+@sha256:[0-9a-f]{64}"')


class RuntimeImagesTest(unittest.TestCase):
    """Known OS and JDK vulnerabilities: each runtime stage is a current base
    image with the OS fixes applied at build time, and runs as a non-root uid."""
    def test_debezium_runs_on_the_current_jre_not_upstreams_ubi8(self):
        ins = _instructions(_read('data-plane/debezium-server/Dockerfile'))
        froms = [i for i in ins if i.upper().startswith('FROM ')]
        self.assertEqual(froms[-1], 'FROM ${JRE_IMAGE}')
        self.assertIn('COPY --from=base --chown=185:0 /debezium /debezium', ins)
        self.assertEqual([i for i in ins if i.startswith('USER ')], ['USER 185'])
    def test_every_runtime_stage_takes_os_fixes(self):
        for rel, cmd in (('tools/go-sink/Dockerfile', 'apk upgrade --no-cache'),
                         ('frontend/Dockerfile.prod', 'apk upgrade --no-cache'),
                         ('data-plane/bulk-loader/Dockerfile', 'apk upgrade --no-cache'),
                         ('backend/Dockerfile.release', 'apk upgrade --no-cache'),
                         ('data-plane/debezium-server/Dockerfile', 'apt-get -y --no-install-recommends upgrade')):
            ins = _instructions(_read(rel))
            last_from = max(i for i, x in enumerate(ins) if x.upper().startswith('FROM '))
            self.assertTrue(any(cmd in x for x in ins[last_from:]), rel)
    def test_the_go_toolchain_is_a_supported_release(self):
        text = _read('tools/go-sink/Dockerfile')
        self.assertRegex(text, r'golang:1\.27@sha256:[0-9a-f]{64}')
        self.assertRegex(text, r'alpine:3\.24@sha256:[0-9a-f]{64}')
    def test_python_images_are_alpine(self):
        # Debian 13's glibc has open findings marked no-fix; musl has none.
        self.assertIn('python:3.11-alpine3.24@sha256:', _read('backend/Dockerfile.release'))
        self.assertIn('python:3.12-alpine3.24@sha256:', _read('data-plane/bulk-loader/Dockerfile'))
    def test_release_builds_take_todays_os_fixes(self):
        builds = re.findall(r'docker buildx build[^\n]*', _read('release.sh'))
        self.assertEqual(len(builds), 5)
        for line in builds:
            self.assertIn('--no-cache', line)
    def test_the_ui_builds_with_node_22(self):
        for rel in ('frontend/Dockerfile', 'frontend/Dockerfile.prod'):
            self.assertIn('node:22-alpine@sha256:', _read(rel), rel)


class CaBundleTest(unittest.TestCase):
    """Amazon RDS's CA bundle is downloaded and checksum-checked, never
    committed: both images and `make ca-bundle` use the same URL and SHA-256."""
    URL = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'

    def _pins(self, rel, pattern):
        found = re.findall(pattern, _read(rel), re.M)
        self.assertEqual(len(found), 1, rel)
        return found[0]

    def test_the_same_pin_everywhere(self):
        pins = {self._pins(rel, r'^ARG RDS_CA_SHA256=([0-9a-f]{64})$')
                for rel in ('data-plane/bulk-loader/Dockerfile', 'backend/Dockerfile.release')}
        pins.add(self._pins('Makefile', r'^RDS_CA_SHA256 := ([0-9a-f]{64})$'))
        self.assertEqual(len(pins), 1, pins)
        for rel in ('data-plane/bulk-loader/Dockerfile', 'backend/Dockerfile.release'):
            self.assertIn('ARG RDS_CA_URL=' + self.URL, _read(rel), rel)
            self.assertIn('hashlib.sha256(d).hexdigest()', _read(rel), rel)
        self.assertIn('RDS_CA_URL := ' + self.URL, _read('Makefile'))

    def test_no_certificate_file_is_committed(self):
        import subprocess  # nosec B404 - reads the repo's own index
        run = subprocess.run(['git', 'ls-files', '--', '*.pem'], cwd=ROOT,  # nosec B603 B607
                             capture_output=True, text=True, check=False)
        if run.returncode != 0:
            self.skipTest('not a git checkout')
        self.assertEqual(run.stdout.split(), [])

    def test_make_test_fetches_it_first(self):
        self.assertRegex(_read('Makefile'), r'(?m)^test: ca-bundle ')


class LockFilesTest(unittest.TestCase):
    """Jira story F181 (hashed lock files) and Jira story F185 (pinned versions)."""

    def test_requirements_pin_exact_versions(self):
        for req, _, _ in PYTHON_IMAGES:
            for line in _read(req).splitlines():
                line = line.split('#', 1)[0].strip()
                if line:
                    self.assertRegex(line, r'^[A-Za-z0-9_.-]+==[0-9][^\s]*$', req)

    def test_the_lock_has_every_pin_at_the_same_version(self):
        for req, lock, _ in PYTHON_IMAGES:
            wanted, locked = _pins(_read(req)), _pins(_read(lock))
            self.assertTrue(wanted, req)
            for name, version in wanted.items():
                self.assertEqual(locked.get(name), version, '{} {}'.format(lock, name))

    def test_every_locked_package_has_a_hash(self):
        for _, lock, _ in PYTHON_IMAGES:
            entries = re.split(r'\n(?=[a-z0-9])', _read(lock))
            packages = [e for e in entries if re.match(r'^[a-z0-9][a-z0-9_.-]*==', e)]
            self.assertTrue(packages, lock)
            for entry in packages:
                self.assertIn('--hash=sha256:', entry, entry.splitlines()[0])

    def test_images_install_the_lock_with_hashes_required(self):
        for _, _, dockerfiles in PYTHON_IMAGES:
            for rel in dockerfiles:
                runs = [i for i in _instructions(_read(rel)) if 'pip install' in i]
                self.assertEqual(len(runs), 1, rel)
                self.assertIn('--require-hashes', runs[0], rel)
                self.assertIn('requirements.lock', runs[0], rel)

    def test_api_dependencies_are_past_the_known_cves(self):
        pins = _pins(_read('backend/requirements.lock'))
        # Flask-CORS: CVE-2024-1681, -6221, -6839, -6844, -6866 (fixed in 6.0.0).
        self.assertGreaterEqual(_version(pins['flask-cors']), (6, 0, 0))
        # requests: CVE-2024-35195, CVE-2024-47081, CVE-2026-25645 (2.33.0).
        self.assertGreaterEqual(_version(pins['requests']), (2, 33, 0))
        # Flask: CVE-2026-27205 (3.1.3). Werkzeug: up to CVE-2026-102598 (3.1.9).
        self.assertGreaterEqual(_version(pins['flask']), (3, 1, 3))
        self.assertGreaterEqual(_version(pins['werkzeug']), (3, 1, 9))
        # PyMySQL: CVE-2024-36039 (1.1.1).
        self.assertGreaterEqual(_version(pins['pymysql']), (1, 1, 1))


class NonRootTest(unittest.TestCase):
    """Jira story F184."""

    def _user(self, rel):
        users = [i.split(None, 1)[1] for i in _instructions(_read(rel))
                 if i.upper().startswith('USER ')]
        self.assertTrue(users, '{} has no USER'.format(rel))
        return users[-1]

    def test_the_api_images_run_as_uid_10001(self):
        for rel in ('backend/Dockerfile', 'backend/Dockerfile.release'):
            self.assertEqual(self._user(rel), '10001', rel)
            self.assertIn('chown 10001:10001 /app/data', _read(rel), rel)

    def test_no_image_ends_as_root(self):
        for rel in DOCKERFILES:
            if rel == 'data-plane/debezium-server/Dockerfile':
                continue        # upstream's own non-root user (no USER line here)
            self.assertNotIn(self._user(rel), ('root', '0', '0:0'), rel)

    def test_the_control_plane_gives_the_data_directory_to_that_uid(self):
        script = _template()['Resources']['Instance']['Properties']['UserData'][
            'Fn::Base64']['Sub'][0]
        mkdir = script.index('mkdir -p /var/lib/ecdl/data')
        chown = script.index('chown -R 10001:10001 /var/lib/ecdl/data')
        self.assertLess(mkdir, chown)
        self.assertLess(chown, script.index('docker run -d --name ecdl-backend'))

    def test_the_compose_build_context_leaves_data_out(self):
        ignore = [l.strip() for l in _read('backend/.dockerignore').splitlines()]
        self.assertIn('data/', ignore)
        release = [l.strip() for l in _read('backend/Dockerfile.release.dockerignore').splitlines()]
        self.assertIn('backend/data', release)


if __name__ == '__main__':
    unittest.main()
