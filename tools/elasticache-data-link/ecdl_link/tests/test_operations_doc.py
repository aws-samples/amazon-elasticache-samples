"""docs/operations.md states the sink's numbers and limits as the code has them.

The runbook said a dead letter came after 5 attempts (it is the first try and
5 retries), that `stuck` "usually clears in 45-60s", that "nothing is dropped
silently", and gave no worker count while the template and the sink binary
disagree (8 and 16). Each check here reads the number from the code and looks
for it in the text, so a change to either side fails here (Jira story F196).
"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
SINK = os.path.join(ROOT, 'tools', 'go-sink')


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as handle:
        return handle.read()


def go_default(name, file='reliability.go'):
    found = re.search(r'atoiOr\("{}", (\d+)\)'.format(name), read('tools', 'go-sink', file))
    return int(found.group(1))


def flat(text):
    """The doc with line breaks folded, so a phrase can wrap anywhere."""
    return re.sub(r'\s+', ' ', text)


class OperationsDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ops = flat(read('docs', 'operations.md'))

    def test_dead_letter_counts_the_first_try_and_the_retries(self):
        n = go_default('MAX_DELIVERIES')
        self.assertIn('on its first try and on all `MAX_DELIVERIES` (default {}) retries, '
                      '{} tries in all'.format(n, n + 1), self.ops)
        self.assertIn('on its first try and on all `MAX_DELIVERIES` ({}) retries'.format(n),
                      self.ops)
        self.assertNotIn('(default {}) attempts'.format(n), self.ops)
        # The code: dead-lettered once the retries (deliveries - 1) reach it.
        rel = read('tools', 'go-sink', 'reliability.go')
        self.assertIn('deliveries[p.ID] = p.RetryCount - 1', rel)
        self.assertIn('case retries >= maxDeliveries:', rel)

    def test_stuck_row_uses_the_monitor_and_reclaim_numbers(self):
        monitor = read('backend', 'monitor.py')
        stuck_ms = int(re.search(r"STUCK_WARN_MS = _env_int\('DEMO_STUCK_WARN_MS', (\d+)\)",
                                 monitor).group(1))
        idle = go_default('RETRY_IDLE_MS') // 1000
        every = go_default('RECLAIM_INTERVAL_MS') // 1000
        batch = go_default('RECLAIM_BATCH')
        row = re.search(r'\| `stuck` \|[^\n]*', read('docs', 'operations.md')).group(0)
        self.assertIn('more than {} s'.format(stuck_ms // 1000), row)
        self.assertIn('every {}–{} s'.format(idle, idle + every), row)
        self.assertIn('`RETRY_IDLE_MS` {} s, checked every {} s'.format(idle, every), row)
        self.assertIn('`RECLAIM_BATCH` {}'.format(batch), row)
        self.assertNotIn('usually clears', row)

    def test_worker_counts_match_the_template_the_provisioner_and_the_binary(self):
        from ecdl_link import provision
        template = read('infra', 'ecdl-cdc-ecs.yaml')
        tmpl = int(re.search(r"\n  SinkWorkers:\n    Type: String\n    Default: '(\d+)'",
                             template).group(1))
        deployed = provision.DEFAULT_SINK['workers']
        self.assertEqual(tmpl, deployed)
        binary = int(re.search(r'(?:envOr|atoiOr)\("WORKERS", "?(\d+)"?\)',
                               read('tools', 'go-sink', 'main.go')).group(1))
        self.assertIn('A deployed link runs {} workers with batches of {}'.format(
            deployed, provision.DEFAULT_SINK['batch_size']), self.ops)
        self.assertIn('the sink binary run by hand defaults to {} workers'.format(binary),
                      self.ops)

    def test_the_errors_named_as_an_outage_are_the_ones_the_sink_holds(self):
        writes = read('tools', 'go-sink', 'writes.go')
        body = writes.split('func targetWide(', 1)[1].split('\n}\n', 1)[0]
        held = set(re.search(r'for _, p := range \[\]string\{([^}]*)\}', body)
                   .group(1).replace('"', '').replace(',', ' ').split())
        section = self.ops.split('## Sink is behind', 1)[1].split('\n## ', 1)[0]
        bullet = section.split('**At the scaling ceiling:**', 1)[0]
        named = set(re.findall(r'`([A-Z]{3,})`', bullet)) - {'WORKERS', 'BATCH_SIZE', 'ERROR', 'PING'}
        self.assertTrue(named)
        self.assertEqual(sorted(named - held), [])
        # Holding is decided by a PING of every shard, as the text says.
        rel = read('tools', 'go-sink', 'reliability.go')
        self.assertIn('cc.ForEachShard', rel)
        self.assertIn('while any shard does not answer a `PING`, reclaim claims nothing',
                      bullet)

    def test_delivery_guarantees_are_qualified(self):
        self.assertNotIn('dead-lettered. Nothing is dropped silently:', self.ops)
        self.assertIn('These are the known exceptions:', self.ops)
        template = read('infra', 'ecdl-cdc-ecs.yaml')
        for alarm in ('pending-vanished', 'target-evicted', 'dead-letters'):
            self.assertIn("-{}'".format(alarm), template, alarm)
            self.assertIn('`…-{}`'.format(alarm), self.ops, alarm)
        for switch in ('TARGET_WAIT_REPLICAS', 'VERSION_GUARD'):
            self.assertIn('`{}'.format(switch), self.ops, switch)


if __name__ == '__main__':
    unittest.main()
