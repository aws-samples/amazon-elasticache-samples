"""Jira story F195: the broker template says what it guarantees, and no more.

It used to call the broker a durable buffer and promise about a million
queued events on the default node type. The guarantee is the replica (a
failover loses nothing committed) and the daily snapshot (losing both nodes
falls back to it).
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link.safe_yaml import load_cfn  # noqa: E402

BROKER = os.path.join(ROOT, 'infra', 'ecdl-broker-quickstart.yaml')


class BrokerClaimsTest(unittest.TestCase):
    def setUp(self):
        with open(BROKER) as handle:
            self.text = handle.read()
        self.t = load_cfn(self.text)

    def test_no_durability_or_capacity_promise(self):
        shown = ' '.join([self.t['Description'], self.t['Parameters']['NodeType']['Description'],
                          self.t['Parameters']['SnapshotRetentionLimit']['Description']]).lower()
        for claim in ('durable', 'million'):
            self.assertNotIn(claim, shown)

    def test_states_the_failover_and_snapshot_guarantee(self):
        desc = ' '.join(self.t['Description'].split())
        self.assertIn('A failover loses nothing committed', desc)
        self.assertIn('daily snapshot', desc)
        self.assertIn('up to a day old', self.t['Parameters']['SnapshotRetentionLimit']['Description'])
        self.assertIn('WHAT IT GUARANTEES', self.text)

    def test_the_snapshot_default_matches_the_wording(self):
        self.assertEqual(self.t['Parameters']['SnapshotRetentionLimit']['Default'], 3)
        self.assertEqual(self.t['Resources']['Broker']['DeletionPolicy'], 'Snapshot')
        # two nodes unless NodeCount says otherwise
        self.assertEqual(self.t['Resources']['Broker']['Properties']['NumCacheClusters'],
                         {'If': ['HasNodeCount', {'Ref': 'NodeCount'}, 2]})


if __name__ == '__main__':
    unittest.main()
