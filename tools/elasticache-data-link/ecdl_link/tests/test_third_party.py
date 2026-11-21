"""THIRD-PARTY-LICENSES lists what the images ship, at the pinned versions.
A dependency added to a lock file, package.json or go.mod without a line in
THIRD-PARTY-LICENSES fails here, so the file cannot quietly fall behind.
"""
import json
import os
import re
import unittest

from ecdl_link.tests.test_control_plane_iam import ROOT


def _read(rel):
    with open(os.path.join(ROOT, rel)) as handle:
        return handle.read()


class ThirdPartyLicensesTest(unittest.TestCase):
    def setUp(self):
        self.text = _read('THIRD-PARTY-LICENSES')
        self.rows = {}
        for line in self.text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                self.rows.setdefault(parts[0], parts[1])

    def test_every_python_package_at_its_pinned_version(self):
        for lock in ('backend/requirements.lock', 'data-plane/bulk-loader/requirements.lock'):
            for name, version in re.findall(r'^([A-Za-z0-9_.-]+)==([^\s;\\]+)', _read(lock), re.M):
                self.assertEqual(self.rows.get(name.lower()), version, '{} {}'.format(lock, name))

    def test_every_ui_dependency_that_ships(self):
        lock = json.loads(_read('frontend/package-lock.json'))
        for path, pkg in lock['packages'].items():
            if not path.startswith('node_modules/') or pkg.get('dev') or pkg.get('devOptional'):
                continue
            name = path.split('node_modules/')[-1]
            self.assertEqual(self.rows.get(name), pkg['version'], name)

    def test_every_go_module_the_sink_requires(self):
        for path, version in re.findall(r'^\s+(\S+) (v\S+)', _read('tools/go-sink/go.mod'), re.M):
            if path.startswith('github.com/aws/aws-sdk-go-v2'):
                self.assertIn('github.com/aws/aws-sdk-go-v2/...', self.rows, path)
            else:
                self.assertEqual(self.rows.get(path), version, path)

    def test_every_runtime_base_image(self):
        for rel in ('backend/Dockerfile.release', 'data-plane/bulk-loader/Dockerfile',
                    'tools/go-sink/Dockerfile', 'frontend/Dockerfile.prod'):
            final = re.findall(r'^FROM (?:--\S+ )?public\.ecr\.aws/docker/library/([^@\s]+)@', _read(rel), re.M)[-1]
            self.assertIn(final, self.rows, rel)
        self.assertIn('eclipse-temurin:21-jre', self.rows)
        self.assertIn('Debezium Server 3.0.6.Final', self.text)

    def test_the_readme_status_names_this_release(self):
        version = _read('VERSION').strip()
        status = [l for l in _read('README.md').splitlines() if l.startswith('**Status:**')]
        self.assertEqual(len(status), 1)
        self.assertIn('release {} (`VERSION`)'.format(version), status[0])

    def test_the_readme_links_it(self):
        self.assertIn('[THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES)', _read('README.md'))
        # The disclaimer aws-samples requires, right under the title.
        first = [l for l in _read('README.md').splitlines() if l.strip()][:2]
        self.assertEqual(first[0], '# ElastiCache Data Link (ECDL)')
        self.assertTrue(first[1].startswith('> **This is sample code, for non-production usage.**'))


if __name__ == '__main__':
    unittest.main()
