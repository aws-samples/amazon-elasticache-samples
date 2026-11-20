"""The Debezium Server properties files, read as text.

Settings whose absence is silent at run time: nothing fails, the cache is just
wrong. Each test names the finding it guards.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DBZ = os.path.join(ROOT, 'data-plane', 'debezium-server')
SINK = os.path.join(ROOT, 'tools', 'go-sink')


def properties(name):
    out = {}
    with open(os.path.join(DBZ, name)) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _sep, value = line.partition('=')
            out[key.strip()] = value.strip()
    return out


class UnavailableValue(unittest.TestCase):  # Jira story F035

    def test_the_placeholder_is_stated_and_overridable(self):
        value = properties('application.properties')[
            'debezium.source.unavailable.value.placeholder']
        self.assertEqual(value,
                         '${UNAVAILABLE_VALUE_PLACEHOLDER:__debezium_unavailable_value}')

    def test_the_sink_reads_the_same_variable_with_the_same_default(self):
        with open(os.path.join(SINK, 'unavailable.go')) as handle:
            go = handle.read()
        self.assertIn('envOr("UNAVAILABLE_VALUE_PLACEHOLDER", defaultUnavailablePlaceholder)', go)
        default = re.search(r'const defaultUnavailablePlaceholder = "([^"]+)"', go).group(1)
        self.assertEqual(default, '__debezium_unavailable_value')

    def test_oracle_captures_lobs(self):
        self.assertEqual(properties('application-oracle.properties')
                         .get('debezium.source.lob.enabled'), 'true')


class QueueBoundInBytes(unittest.TestCase):  # Jira story F013

    def test_queue_is_bounded_in_bytes_as_well_as_events(self):
        p = properties('application.properties')
        self.assertEqual(p['debezium.source.max.queue.size.in.bytes'],
                         '${MAX_QUEUE_SIZE_BYTES:268435456}')
        # At most half the heap the entrypoint gives a 1 GB task (75%, 768 MB).
        self.assertLessEqual(268435456, 0.75 * 1024 ** 3 / 2)


class PostgresSlotMovesWhileIdle(unittest.TestCase):  # Jira story F011

    def test_heartbeat_writes_a_logical_message_the_broker_never_sees(self):
        p = properties('application-postgresql.properties')
        query = p['debezium.source.heartbeat.action.query']
        match = re.match(r"^\$\{HEARTBEAT_ACTION_QUERY:SELECT pg_logical_emit_message\("
                         r"false, '([^']+)', ''\)\}$", query)
        # Non-transactional (false): sent at once, with no table and no COMMIT.
        self.assertIsNotNone(match, query)
        prefix = match.group(1)
        excluded = p['debezium.source.message.prefix.exclude.list']
        self.assertTrue(any(re.fullmatch(rx.strip(), prefix) for rx in excluded.split(',')),
                        excluded)

    def test_only_postgresql_sets_it(self):
        for name in ('application.properties', 'application-mysql.properties',
                     'application-sqlserver.properties', 'application-oracle.properties'):
            self.assertNotIn('debezium.source.heartbeat.action.query', properties(name), name)

    def test_the_default_has_no_braces_that_would_end_the_expression(self):
        # SmallRye ends ${NAME:default} at the first unmatched '}'.
        query = properties('application-postgresql.properties')[
            'debezium.source.heartbeat.action.query']
        self.assertEqual(query.count('{'), 1)
        self.assertEqual(query.count('}'), 1)


class MysqlProfile(unittest.TestCase):  # Jira stories F012, F016

    def test_snapshot_defaults(self):
        p = properties('application-mysql.properties')
        self.assertEqual(p['debezium.source.snapshot.mode'], '${SNAPSHOT_MODE:no_data}')
        # Upstream's own default when the entrypoint is bypassed; the
        # entrypoint chooses none for no_data (test_capture_entrypoint).
        self.assertEqual(p['debezium.source.snapshot.locking.mode'],
                         '${SNAPSHOT_LOCKING_MODE:minimal}')

    def test_no_profile_uses_the_deprecated_mode_name(self):
        for name in ('application-mysql.properties', 'application-postgresql.properties',
                     'application-sqlserver.properties', 'application-oracle.properties'):
            self.assertNotIn('schema_only', properties(name).get('debezium.source.snapshot.mode', ''),
                             name)


class NoSharedSignalTable(unittest.TestCase):  # Jira story F020

    def test_the_shared_mysql_signal_table_script_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(DBZ, 'setup-signal-table.sql')))
        # The signalling collection is per link and has no default.
        self.assertEqual(properties('application.properties')['debezium.source.signal.data.collection'],
                         '${SIGNAL_COLLECTION}')


if __name__ == '__main__':
    unittest.main()
