"""Tests for the spec diff and repair planner.

The behaviour under test is not "did a field change" -- it is "does the cache end
up correct". Every case here is one where a plain redeploy leaves the cache wrong
in a way the pipeline itself would never report.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.repair import (  # noqa: E402
    describe_diff, describe_repair, diff, repair_plan, signal_for,
)
from ecdl_link.tests.test_provision import resolution  # noqa: E402


def link(mode='initial_load_and_cdc', tables=None, transforms=None, **over):
    doc = {
        'schema_version': 1,
        'name': 'checkout',
        'mode': mode,
        'source': {
            'arn': 'arn:aws:rds:us-east-2:1:cluster:src',
            'database': 'Inventory',
            'secret_arn': 'arn:aws:secretsmanager:us-east-2:1:secret:db',  # nosec B105 - fake test credential
        },
        'target': {'arn': 'arn:aws:elasticache:us-east-2:1:replicationgroup:tgt'},
        'tables': tables if tables is not None else ['orders'],
    }
    if transforms:
        doc['transforms'] = {'tables': transforms}
    doc.update(over)
    return doc


def resolved(tables=('orders',)):
    r = resolution('initial_load_and_cdc')
    r['tables'] = [
        {'input': t, 'table': t, 'schema': None, 'database': 'Inventory',
         'qualified': 'Inventory.{}'.format(t),
         'topic_suffix': 'Inventory.{}'.format(t),
         'stream': 'data:checkout.Inventory.{}'.format(t),
         'key_prefix': '{}:'.format(t)}
        for t in tables]
    return r


def kinds(d):
    return [c['kind'] for c in d['changes']]


def step_kinds(p):
    return [s['kind'] for s in p['steps']]


class NoChangeTest(unittest.TestCase):
    def test_identical_is_no_change(self):
        d = diff(link(), link())
        self.assertFalse(d['changed'])
        self.assertEqual(d['severity'], 'none')

    def test_reordered_tables_are_not_a_change(self):
        # A diff that reports formatting gets ignored, and then the real changes
        # get ignored with it.
        a = link(tables=['orders', 'customers'])
        b = link(tables=['customers', 'orders'])
        self.assertFalse(diff(a, b)['changed'],
                         'table order is not a semantic change')

    def test_repair_of_no_change_does_nothing(self):
        p = repair_plan(diff(link(), link()), resolved())
        self.assertEqual(p['steps'], [])
        self.assertEqual(p['severity'], 'none')


class TableAddedTest(unittest.TestCase):
    def setUp(self):
        self.d = diff(link(tables=['orders']),
                      link(tables=['orders', 'customers']))

    def test_reported_as_needing_a_backfill(self):
        # This is the case a plain redeploy gets wrong: the new table streams only
        # future changes, so every pre-existing row is permanently missing.
        self.assertEqual(kinds(self.d), ['table_added'])
        self.assertEqual(self.d['severity'], 'backfill')

    def test_consequence_names_the_missing_rows(self):
        self.assertIn('already exists', self.d['changes'][0]['consequence'])

    def test_plan_updates_the_stack_then_snapshots(self):
        p = repair_plan(self.d, resolved(('orders', 'customers')))
        self.assertEqual(step_kinds(p), ['deploy', 'signal'])

    def test_snapshot_names_only_the_new_table_qualified(self):
        p = repair_plan(self.d, resolved(('orders', 'customers')))
        signal = next(s for s in p['steps'] if s['kind'] == 'signal')
        self.assertEqual(signal['collections'], ['Inventory.customers'])

    def test_deploy_comes_before_the_snapshot(self):
        # A snapshot requested before the connector knows the table is rejected as
        # an unknown collection.
        p = repair_plan(self.d, resolved(('orders', 'customers')))
        self.assertLess(step_kinds(p).index('deploy'),
                        step_kinds(p).index('signal'))

    def test_signal_is_an_incremental_snapshot(self):
        p = repair_plan(self.d, resolved(('orders', 'customers')))
        signal = signal_for(next(s for s in p['steps'] if s['kind'] == 'signal'))
        self.assertEqual(signal['type'], 'execute-snapshot')
        self.assertIn('incremental', signal['data'])


class TableRemovedTest(unittest.TestCase):
    def setUp(self):
        self.d = diff(link(tables=['orders', 'customers']),
                      link(tables=['orders']))

    def test_reported_as_needing_cleanup(self):
        self.assertEqual(kinds(self.d), ['table_removed'])
        self.assertEqual(self.d['severity'], 'cleanup')

    def test_consequence_names_the_stale_keys(self):
        # The point is that nothing cleans them up on its own, so the operator has
        # to be told rather than left to discover it.
        consequence = self.d['changes'][0]['consequence']
        self.assertIn('stay in the cache', consequence)
        self.assertIn('permanently stale', consequence)

    def test_plan_lists_the_key_pattern(self):
        p = repair_plan(self.d, resolved(('orders',)))
        cleanup = next(s for s in p['steps'] if s['kind'] == 'cleanup')
        self.assertEqual(cleanup['key_patterns'], ['customers:*'])

    def test_cleanup_requires_confirmation(self):
        # Deleting cache keys is destructive and only the customer knows whether
        # anything still reads them.
        p = repair_plan(self.d, resolved(('orders',)))
        cleanup = next(s for s in p['steps'] if s['kind'] == 'cleanup')
        self.assertTrue(cleanup['confirm'])
        self.assertTrue(any('destructive' in n for n in p['notes']))

    def test_no_snapshot_for_a_removal(self):
        p = repair_plan(self.d, resolved(('orders',)))
        self.assertNotIn('signal', step_kinds(p))


class TransformChangeTest(unittest.TestCase):
    """Every one of these leaves rows already in the cache with the old shape."""

    def _d(self, before, after):
        return diff(link(transforms={'orders': before} if before else None),
                    link(transforms={'orders': after} if after else None))

    def test_computed_field_changed_needs_a_backfill(self):
        d = self._d({'computed': [{'name': 'c', 'expr': 'price * 100'}]},
                    {'computed': [{'name': 'c', 'expr': 'price * 110'}]})
        self.assertEqual(kinds(d), ['transform_changed'])
        self.assertEqual(d['severity'], 'backfill')
        self.assertIn('old expression', d['changes'][0]['consequence'])

    def test_filter_added_leaves_rows_that_should_be_gone(self):
        # The harder case: no future event will remove them.
        d = self._d(None, {'filter': {'field': 'status', 'op': '=',
                                      'value': 'active'}})
        self.assertIn('nothing will remove them', d['changes'][0]['consequence'])

    def test_filter_removed_leaves_rows_that_should_be_present(self):
        d = self._d({'filter': {'field': 'status', 'op': '=', 'value': 'active'}},
                    None)
        self.assertIn('none of them are', d['changes'][0]['consequence'])

    def test_columns_changed(self):
        d = self._d({'columns': ['id', 'total']}, {'columns': ['id']})
        self.assertIn('selected columns', d['changes'][0]['consequence'])

    def test_rename_changed(self):
        d = self._d({'rename': {'total': 'amount'}}, {'rename': {'total': 'value'}})
        self.assertIn('old field name', d['changes'][0]['consequence'])

    def test_identical_transform_is_not_a_change(self):
        spec = {'computed': [{'name': 'c', 'expr': 'price * 100'}]}
        self.assertFalse(self._d(spec, dict(spec))['changed'])

    def test_plan_snapshots_the_affected_table(self):
        d = self._d({'columns': ['id']}, {'columns': ['id', 'total']})
        p = repair_plan(d, resolved(('orders',)))
        signal = next(s for s in p['steps'] if s['kind'] == 'signal')
        self.assertEqual(signal['collections'], ['Inventory.orders'])

    def test_transform_of_a_newly_added_table_is_not_double_reported(self):
        # The table addition already implies its transform; reporting both would
        # request the same snapshot twice, which is a full extra re-read.
        d = diff(link(tables=['orders']),
                 link(tables=['orders', 'customers'],
                      transforms={'customers': {'columns': ['id']}}))
        self.assertEqual(kinds(d), ['table_added'])

    def test_transform_of_a_removed_table_is_not_reported_separately(self):
        d = diff(link(tables=['orders', 'customers'],
                      transforms={'customers': {'columns': ['id']}}),
                 link(tables=['orders']))
        self.assertEqual(kinds(d), ['table_removed'])


class DeduplicationTest(unittest.TestCase):
    def test_one_signal_for_several_tables(self):
        d = diff(link(tables=['orders']),
                 link(tables=['orders', 'customers', 'products']))
        p = repair_plan(d, resolved(('orders', 'customers', 'products')))
        signals = [s for s in p['steps'] if s['kind'] == 'signal']
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['collections'],
                         ['Inventory.customers', 'Inventory.products'])

    def test_added_and_transformed_table_snapshotted_once(self):
        d = diff(link(tables=['orders'], transforms={'orders': {'columns': ['id']}}),
                 link(tables=['orders', 'customers'],
                      transforms={'orders': {'columns': ['id', 'total']}}))
        p = repair_plan(d, resolved(('orders', 'customers')))
        signal = next(s for s in p['steps'] if s['kind'] == 'signal')
        self.assertEqual(sorted(signal['collections']),
                         ['Inventory.customers', 'Inventory.orders'])
        self.assertEqual(len(signal['collections']), 2, 'no duplicates')


class BlockedChangeTest(unittest.TestCase):
    """Changes that cannot be applied in place, and must say so rather than try."""

    def test_rename_is_blocked(self):
        d = diff(link(), link(name='checkout2'))
        self.assertEqual(d['severity'], 'recreate')
        p = repair_plan(d, resolved())
        self.assertTrue(p['blocked'])
        self.assertEqual(p['steps'], [])

    def test_source_change_is_blocked(self):
        a = link()
        b = link()
        b['source'] = dict(b['source'], arn='arn:aws:rds:us-east-2:1:cluster:other')
        d = diff(a, b)
        self.assertEqual(d['severity'], 'recreate')
        self.assertIn('log position', d['changes'][0]['consequence'])

    def test_target_change_is_blocked(self):
        a = link()
        b = link()
        b['target'] = {'arn': 'arn:aws:elasticache:us-east-2:1:replicationgroup:t2'}
        d = diff(a, b)
        self.assertEqual(d['severity'], 'recreate')
        self.assertIn('empty', d['changes'][0]['consequence'])

    def test_blocked_plan_explains_rather_than_half_applying(self):
        p = repair_plan(diff(link(), link(name='other')), resolved())
        text = describe_repair(p)
        self.assertIn('BLOCKED', text)
        self.assertIn('cannot be applied', text)


class ModeChangeTest(unittest.TestCase):
    def test_gaining_cdc_needs_a_full_backfill(self):
        # Capture starts at the current log position, so everything written since
        # the last load is missing.
        d = diff(link(mode='initial_load'), link(mode='initial_load_and_cdc'))
        self.assertEqual(d['severity'], 'backfill')
        p = repair_plan(d, resolved(('orders',)))
        signal = next(s for s in p['steps'] if s['kind'] == 'signal')
        self.assertEqual(signal['collections'], ['Inventory.orders'])

    def test_losing_cdc_is_refused_in_place(self):
        # Nothing removed the capture stacks, and the load ran unpaused under
        # live capture (lifecycle H2).
        for before in ('initial_load_and_cdc', 'cdc'):
            d = diff(link(mode=before), link(mode='initial_load'))
            self.assertEqual(d['severity'], 'recreate', before)
            self.assertIn('destroy', d['changes'][0]['action'])
            self.assertTrue(repair_plan(d, resolved())['blocked'])

    def test_dropping_only_the_load_is_just_a_redeploy(self):
        d = diff(link(mode='initial_load_and_cdc'), link(mode='cdc'))
        self.assertEqual(d['severity'], 'deploy')


class AdvancedChangeTest(unittest.TestCase):
    def test_scaling_change_is_only_a_redeploy(self):
        d = diff(link(), link(advanced={'sink': {'workers': 32}}))
        self.assertEqual(d['severity'], 'deploy')
        self.assertIn('not what is in the cache', d['changes'][0]['consequence'])

    def test_no_snapshot_for_a_scaling_change(self):
        p = repair_plan(diff(link(), link(advanced={'sink': {'workers': 32}})),
                        resolved())
        self.assertEqual(step_kinds(p), ['deploy'])


class KeyPrefixChangeTest(unittest.TestCase):
    """Lifecycle H6: a prefix change moves every key; it was "deploy only"."""

    def test_adding_a_prefix_needs_snapshot_and_cleanup(self):
        d = diff(link(tables=['orders', 'customers']),
                 link(tables=['orders', 'customers'], advanced={'key_prefix': 'b'}))
        self.assertEqual(d['severity'], 'cleanup')
        self.assertEqual(kinds(d), ['key_prefix'])
        p = repair_plan(d, resolved(('orders', 'customers')))
        signal = next(s for s in p['steps'] if s['kind'] == 'signal')
        self.assertEqual(sorted(signal['collections']),
                         ['Inventory.customers', 'Inventory.orders'])
        cleanup = next(s for s in p['steps'] if s['id'] == 'old_prefix_keys')
        self.assertEqual(sorted(cleanup['key_patterns']), ['customers:*', 'orders:*'])
        self.assertTrue(cleanup['confirm'])

    def test_changing_a_prefix_cleans_the_old_one(self):
        d = diff(link(advanced={'key_prefix': 'b:'}), link(advanced={'key_prefix': 'c'}))
        cleanup = next(s for s in repair_plan(d, resolved())['steps']
                       if s['id'] == 'old_prefix_keys')
        self.assertEqual(cleanup['key_patterns'], ['b:orders:*'])

    def test_same_prefix_spelled_differently_is_no_change(self):
        self.assertFalse(diff(link(advanced={'key_prefix': 'b'}),
                              link(advanced={'key_prefix': 'b:'}))['changed'])

    def test_removed_table_pattern_carries_the_prefix(self):
        # Lifecycle M8: '<table>:*' on a prefixed link named another link's keys.
        d = diff(link(tables=['orders', 'customers'], advanced={'key_prefix': 'b'}),
                 link(tables=['orders'], advanced={'key_prefix': 'b'}))
        cleanup = next(s for s in repair_plan(d, resolved())['steps']
                       if s['kind'] == 'cleanup')
        self.assertEqual(cleanup['key_patterns'], ['b:customers:*'])


class MissingSignallingTest(unittest.TestCase):
    def test_backfill_without_a_signal_collection_is_called_out(self):
        # Without the signalling table the backfill cannot be requested at all, and
        # the rows would silently stay wrong.
        r = resolved(('orders', 'customers'))
        r['debezium'] = dict(r['debezium'], signal_collection=None)
        p = repair_plan(diff(link(tables=['orders']),
                             link(tables=['orders', 'customers'])), r)
        self.assertTrue(any('signalling' in n for n in p['notes']),
                        p['notes'])


class SeverityTest(unittest.TestCase):
    def test_worst_severity_wins(self):
        d = diff(link(tables=['orders']),
                 link(tables=['orders', 'customers'],
                      advanced={'sink': {'workers': 32}}))
        # backfill outranks deploy.
        self.assertEqual(d['severity'], 'backfill')

    def test_recreate_outranks_everything(self):
        d = diff(link(tables=['orders']),
                 link(name='other', tables=['orders', 'customers']))
        self.assertEqual(d['severity'], 'recreate')


class DescribeTest(unittest.TestCase):
    def test_diff_states_the_consequence_not_just_the_field(self):
        text = describe_diff(diff(link(tables=['orders']),
                                  link(tables=['orders', 'customers'])))
        self.assertIn('customers', text)
        self.assertIn('why:', text)
        self.assertIn('do:', text)

    def test_no_change_says_so(self):
        self.assertEqual(describe_diff(diff(link(), link())), 'no changes')

    def test_repair_marks_the_step_needing_confirmation(self):
        p = repair_plan(diff(link(tables=['orders', 'customers']),
                             link(tables=['orders'])), resolved(('orders',)))
        self.assertIn('needs confirmation', describe_repair(p))


class OutstandingStepsTest(unittest.TestCase):
    """What a person still has to run after an update. Updates themselves go
    through the one change path (ecdl_link.change, /deploy, `ecdl apply`);
    apply_repair, which updated stacks with none of its checks, is gone."""

    def added_table(self, res=None):
        before = link(tables=['orders'])
        after = link(tables=['orders', 'customers'])
        return repair_plan(diff(before, after), res if res is not None
                           else resolved(('orders', 'customers')))

    def outstanding(self, plan_doc, engine='mysql'):
        from ecdl_link.repair import outstanding_steps
        return outstanding_steps(plan_doc, engine)

    def test_the_signal_is_returned_not_executed(self):
        signals = [o for o in self.outstanding(self.added_table()) if o['kind'] == 'signal']
        self.assertEqual(len(signals), 1)
        self.assertIn('customers', ' '.join(signals[0]['collections']))
        self.assertTrue(signals[0]['reason_not_automatic'])

    def test_the_outstanding_signal_carries_a_runnable_statement(self):
        r = resolved(('orders', 'customers'))
        r.setdefault('debezium', {})['signal_collection'] = 'Inventory.sig_x'
        signal = [o for o in self.outstanding(self.added_table(r)) if o['kind'] == 'signal'][0]
        self.assertIn('execute-snapshot', str(signal['signal']))
        self.assertIn('INSERT INTO Inventory.sig_x', signal['statement'])
        self.assertEqual(len(signal['values']), 3)

    def test_the_statement_uses_the_engines_placeholder_style(self):
        for engine, expected in (('mysql', '%s'), ('oracle', ':1')):
            r = resolved(('orders', 'customers'))
            r.setdefault('debezium', {})['signal_collection'] = 'S.SIG'
            signal = [o for o in self.outstanding(self.added_table(r), engine)
                      if o['kind'] == 'signal'][0]
            self.assertIn(expected, signal['statement'], engine)

    def test_a_missing_signalling_collection_is_called_out(self):
        r = resolved(('orders', 'customers'))
        r.setdefault('debezium', {})['signal_collection'] = None
        signal = [o for o in self.outstanding(self.added_table(r)) if o['kind'] == 'signal'][0]
        self.assertIsNone(signal['statement'])
        self.assertIn('signalling table', signal['blocked_reason'])

    def test_cleanup_is_outstanding_and_says_why(self):
        d = diff(link(tables=['orders', 'customers']), link(tables=['orders']))
        cleanups = [o for o in self.outstanding(repair_plan(d, resolved(('orders',))))
                    if o['kind'] == 'cleanup']
        self.assertEqual(len(cleanups), 1)
        self.assertIn('customers:*', cleanups[0]['key_patterns'])
        self.assertIn('destructive', cleanups[0]['reason_not_automatic'])

    def test_apply_repair_is_gone(self):
        import ecdl_link.repair as repair
        self.assertFalse(hasattr(repair, 'apply_repair'))


if __name__ == '__main__':
    result = unittest.main(exit=False, verbosity=0).result
    print('ok: {} repair tests passed'.format(result.testsRun)
          if result.wasSuccessful() else 'FAILED')
    sys.exit(0 if result.wasSuccessful() else 1)


