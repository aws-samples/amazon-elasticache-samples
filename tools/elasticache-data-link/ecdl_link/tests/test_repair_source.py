"""Jira story F126: `diff` reports a changed schema, external source and credential.

They used to report "no changes", so an edit that moved a link to another
schema or host went through as nothing at all.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.change import review_update  # noqa: E402
from ecdl_link.repair import describe_diff, diff, outstanding_steps, repair_plan  # noqa: E402
from ecdl_link.tests.test_repair import link, resolved  # noqa: E402

SECRET = 'arn:aws:secretsmanager:us-east-2:1:secret:'


def external(**over):
    doc = link(tables=['orders'])
    doc['source'] = dict({'kind': 'external', 'engine': 'postgresql', 'host': 'db.local',
                          'database': 'shop', 'username': 'repl',
                          'secret_arn': SECRET + 'db'}, **over)
    return doc


def with_source(doc, **over):
    doc = copy.deepcopy(doc)
    doc['source'].update(over)
    for key, value in list(doc['source'].items()):
        if value is None:
            doc['source'].pop(key)
    return doc


def change(d, kind, field=None):
    return next(c for c in d['changes'] if c['kind'] == kind
                and (field is None or c['field'] == field))


class ExternalSourceTest(unittest.TestCase):
    def test_host_engine_kind_and_port_cannot_change_in_place(self):
        base = external()
        for over, field in (({'host': 'db2.local'}, 'source.host'),
                            ({'engine': 'mysql'}, 'source.engine'),
                            ({'port': 6432}, 'source.port')):
            d = diff(base, with_source(base, **over))
            c = change(d, 'source', field)
            self.assertEqual((c['severity'], d['severity']), ('recreate', 'recreate'), field)

    def test_the_engines_standard_port_is_no_change(self):
        self.assertFalse(diff(external(), with_source(external(), port=5432))['changed'])

    def test_aws_to_external_is_a_new_source(self):
        aws = link()
        d = diff(aws, external())
        self.assertIn('source.kind', [c['field'] for c in d['changes']
                                      if c['severity'] == 'recreate'])

    def test_an_unset_kind_is_aws(self):
        explicit = with_source(link(), kind='aws')
        self.assertFalse(diff(link(), explicit)['changed'])


class CredentialChangeTest(unittest.TestCase):
    def test_each_credential_is_a_deploy_with_a_reason(self):
        base = link()
        cases = (('source.secret_arn', with_source(base, secret_arn=SECRET + 'db2')),
                 ('source.username', with_source(base, username='cdc')))
        for field, desired in cases:
            d = diff(base, desired)
            c = change(d, 'credential', field)
            self.assertEqual((c['severity'], d['severity']), ('deploy', 'deploy'), field)
            self.assertIn('restart with the new credential', c['consequence'])
        desired = copy.deepcopy(base)
        desired['target']['secret_arn'] = SECRET + 'cache'
        self.assertEqual(change(diff(base, desired), 'credential')['field'], 'target.secret_arn')


class SchemaChangeTest(unittest.TestCase):
    def pg(self, schema=None, tables=('orders',)):
        doc = link(tables=list(tables))
        if schema:
            doc['source']['schema'] = schema
        return doc

    def test_a_schema_change_moves_every_unqualified_table(self):
        d = diff(self.pg('public', ('orders', 'items')), self.pg('sales', ('orders', 'items')))
        moved = [c for c in d['changes'] if c['kind'] == 'table_moved']
        self.assertEqual([(c['table'], c['from'], c['to']) for c in moved],
                         [('items', 'public.items', 'sales.items'),
                          ('orders', 'public.orders', 'sales.orders')])
        self.assertEqual(d['severity'], 'cleanup')
        self.assertIn('never deleted', moved[0]['consequence'])
        self.assertIn('MySQL does not use source.schema', moved[0]['consequence'])

    def test_setting_a_schema_for_the_first_time_moves_from_the_default(self):
        c = change(diff(self.pg(None), self.pg('sales')), 'table_moved')
        self.assertEqual((c['from'], c['to']), ('<default schema>.orders', 'sales.orders'))

    def test_qualified_tables_do_not_move_with_the_schema(self):
        d = diff(self.pg('public', ('reports.orders',)), self.pg('sales', ('reports.orders',)))
        self.assertFalse(d['changed'])

    def test_an_explicit_qualifier_can_move_a_table(self):
        c = change(diff(self.pg(None, ('public.orders',)), self.pg(None, ('sales.orders',))),
                   'table_moved')
        self.assertEqual((c['from'], c['to']), ('public.orders', 'sales.orders'))

    def test_spelling_and_case_are_not_moves(self):
        self.assertFalse(diff(self.pg('public', ('orders',)),
                              self.pg('public', ('Public.orders',)))['changed'])
        self.assertFalse(diff(self.pg(None, ('orders',)),
                              self.pg(None, ('public.orders',)))['changed'])

    def test_mysql_external_ignores_the_schema(self):
        mysql = external(engine='mysql')
        d = diff(with_source(mysql, schema='a'), with_source(mysql, schema='b'))
        self.assertFalse(d['changed'], describe_diff(d))

    def test_the_repair_deletes_the_old_rows_before_the_snapshot(self):
        d = diff(self.pg('public', ('orders',)), self.pg('sales', ('orders',)))
        plan = repair_plan(d, resolved(('orders',)))
        ids = [s['id'] for s in plan['steps']]
        self.assertEqual(ids, ['update_stacks', 'moved_table_keys', 'backfill'])
        cleanup = plan['steps'][1]
        self.assertEqual(cleanup['key_patterns'], ['orders:*'])
        self.assertEqual(plan['steps'][2]['collections'], ['Inventory.orders'])
        kinds = [o['kind'] for o in outstanding_steps(plan, 'mysql')]
        self.assertEqual(kinds, ['cleanup', 'signal'])

    def test_a_key_prefix_is_kept_in_the_cleanup_pattern(self):
        before = self.pg('public')
        before['advanced'] = {'key_prefix': 'v1'}
        after = self.pg('sales')
        after['advanced'] = {'key_prefix': 'v1'}
        plan = repair_plan(diff(before, after), resolved(('orders',)))
        self.assertEqual(plan['steps'][1]['key_patterns'], ['v1:orders:*'])

    def test_postgres_adds_the_moved_table_to_the_publication(self):
        r = resolved(('orders',))
        r['source']['engine'] = 'postgresql'
        r['tables'][0]['qualified'] = 'sales.orders'
        plan = repair_plan(diff(self.pg('public'), self.pg('sales')), r,
                           pg_publication='ecdl_checkout_pub')
        self.assertEqual(plan['steps'][0]['id'], 'publication')
        self.assertIn('sales.orders', plan['steps'][0]['statement'])


class DeployedReviewTest(unittest.TestCase):
    """The review of a deployed link compares qualified names too."""

    def review(self, deployed_tables, planned_tables, comp='cdc'):
        param = 'Tables' if comp == 'cdc' else 'SourceTables'
        deployed = {comp: {param: deployed_tables}}
        plan = {'steps': [{'kind': 'stack', 'id': comp, 'from_outputs': {},
                           'parameters': {param: planned_tables}}]}
        return review_update(plan, {'name': 'l', 'mode': 'cdc' if comp == 'cdc'
                                    else 'initial_load'}, deployed, {}, {})

    def test_a_cdc_table_read_from_another_schema_needs_acknowledgement(self):
        r = self.review('public.orders', 'sales.orders')
        self.assertEqual([c['kind'] for c in r['diff']['changes']], ['table_moved'])
        self.assertTrue(r['needs_ack'])

    def test_a_loader_only_link_uses_its_source_tables(self):
        r = self.review('public.orders', 'sales.orders', comp='loader')
        self.assertEqual([c['kind'] for c in r['diff']['changes']], ['table_moved'])

    def test_an_unchanged_link_is_unchanged(self):
        self.assertFalse(self.review('public.orders', 'public.orders')['diff']['changed'])


if __name__ == '__main__':
    unittest.main()
