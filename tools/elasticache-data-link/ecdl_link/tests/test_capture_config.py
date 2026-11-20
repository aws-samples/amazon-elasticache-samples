"""The Debezium image's configuration names ECDL's own classes, which exist.

The Java tests (broker-writer/src/test, run by the image build) test the classes
themselves; these check the wiring the Java build cannot see, because the image
build compiles broker-writer/ without the properties files next to it.
"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
DBZ = os.path.join(ROOT, 'data-plane', 'debezium-server')
JAVA = os.path.join(DBZ, 'broker-writer', 'src', 'main', 'java')


def properties(name='application.properties'):
    out = {}
    with open(os.path.join(DBZ, name)) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                out[k.strip()] = v.strip()
    return out


def java_class_exists(fqcn):
    return os.path.isfile(os.path.join(JAVA, *fqcn.split('.')) + '.java')


class OffsetStoreWiringTest(unittest.TestCase):

    def test_offset_store_is_ecdls_and_exists(self):
        cls = properties()['debezium.source.offset.storage']
        self.assertEqual(cls, 'io.debezium.server.redis.EcdlOffsetBackingStore')
        self.assertTrue(java_class_exists(cls), cls)

    def test_commit_failure_limit_comes_from_the_environment(self):
        self.assertEqual(
            properties()['debezium.source.offset.storage.ecdl.commit.max.failures'],
            '${OFFSET_COMMIT_MAX_FAILURES:10}')

    def test_offset_store_keeps_the_per_link_key(self):
        self.assertEqual(properties()['debezium.source.offset.storage.redis.key'],
                         '${OFFSET_KEY:ecdl:metadata:offsets}')

    def test_every_offset_store_property_is_one_the_store_reads(self):
        # A misspelt ecdl.* property is silently ignored by Debezium.
        with open(os.path.join(JAVA, 'io', 'debezium', 'server', 'redis',
                               'EcdlOffsetBackingStore.java')) as f:
            source = f.read()
        for key in properties():
            if key.startswith('debezium.source.offset.storage.ecdl.'):
                name = key[len('debezium.source.'):]
                self.assertIn('"{}"'.format(name), source, key)

    def test_the_runbooks_the_refusals_point_to_exist(self):
        # EcdlOffsetBackingStore's FATAL lines send the operator to these.
        with open(os.path.join(JAVA, 'io', 'debezium', 'server', 'redis',
                               'EcdlOffsetBackingStore.java')) as f:
            source = f.read()
        with open(os.path.join(ROOT, 'docs', 'operations.md')) as f:
            ops = f.read()
        self.assertIn("Switchovers, blue/green and major", source)
        self.assertIn('\n## Switchovers, blue/green and major upgrades\n', ops)
        self.assertIn('\n## Debezium refuses to start: no stored offset\n', ops)
        for anchor in re.findall(r'operations\.md#([a-z0-9-]+)', open(
                os.path.join(ROOT, 'docs', 'prerequisites.md')).read()):
            heads = {re.sub(r'[^a-z0-9 -]', '', h.lower()).replace(' ', '-')
                     for h in re.findall(r'^## (.+)$', ops, re.M)}
            self.assertIn(anchor, heads)

    def test_image_build_runs_the_offset_store_tests(self):
        with open(os.path.join(DBZ, 'Dockerfile')) as f:
            dockerfile = f.read()
        self.assertIn('io.debezium.server.redis.EcdlOffsetBackingStoreTest', dockerfile)
        self.assertRegex(dockerfile, re.compile(
            r'[0-9a-f]{64}  /debezium/lib/debezium-storage-redis-3\.0\.6\.Final\.jar'))


class SchemaHistoryWiringTest(unittest.TestCase):  # Jira stories F009, F010, F017
    PREFIX = 'debezium.source.schema.history.internal.'

    def test_history_class_is_ecdls_and_exists(self):
        cls = properties()['debezium.source.schema.history.internal']
        self.assertEqual(cls, 'io.debezium.server.redis.EcdlSchemaHistory')
        self.assertTrue(java_class_exists(cls), cls)

    def test_history_waits_for_a_replica_like_the_offsets(self):
        p = properties()
        self.assertEqual(p[self.PREFIX + 'redis.wait.enabled'], '${BROKER_WAIT:true}')
        self.assertEqual(p['debezium.source.offset.storage.redis.wait.enabled'],
                         p[self.PREFIX + 'redis.wait.enabled'])
        self.assertEqual(p[self.PREFIX + 'ecdl.wait.attempts'], '${SCHEMA_HISTORY_WAIT_ATTEMPTS:30}')

    def test_history_retry_budget_is_minutes_not_seconds(self):
        # 300 ms doubling to 10 s: 30 attempts is about four minutes, the
        # upstream default of 10 about one.
        p = properties()
        self.assertEqual(p[self.PREFIX + 'redis.retry.max.attempts'],
                         '${SCHEMA_HISTORY_RETRY_ATTEMPTS:30}')
        delay, total = 300, 0
        for _ in range(30 - 1):
            total += delay
            delay = min(10000, delay * 2)
        self.assertGreater(total, 3 * 60 * 1000)

    def test_every_ecdl_history_property_is_one_the_class_reads(self):
        with open(os.path.join(JAVA, 'io', 'debezium', 'server', 'redis',
                               'EcdlSchemaHistory.java')) as f:
            source = f.read()
        found = [k for k in properties() if k.startswith(self.PREFIX + 'ecdl.')]
        self.assertTrue(found)
        for key in found:
            self.assertIn('"{}"'.format(key[len(self.PREFIX):]), source, key)

    def test_all_three_broker_connections_verify_the_hostname(self):
        p = properties()
        for key in ('debezium.sink.redis.ssl.hostname.verification.enabled',
                    'debezium.source.offset.storage.redis.ssl.hostname.verification.enabled',
                    self.PREFIX + 'redis.ssl.hostname.verification.enabled'):
            self.assertEqual(p.get(key), 'true', key)

    def test_image_build_runs_the_history_tests(self):
        with open(os.path.join(DBZ, 'Dockerfile')) as f:
            dockerfile = f.read()
        self.assertIn('io.debezium.server.redis.EcdlSchemaHistoryTest', dockerfile)

    def test_the_size_warning_points_at_a_runbook_that_exists(self):
        with open(os.path.join(JAVA, 'io', 'debezium', 'server', 'redis',
                               'EcdlSchemaHistory.java')) as f:
            source = f.read()
        with open(os.path.join(ROOT, 'docs', 'operations.md')) as f:
            ops = f.read()
        self.assertIn("'Schema history size' in docs/operations.md", source)
        self.assertIn('\n## Schema history size\n', ops)


if __name__ == '__main__':
    unittest.main()
