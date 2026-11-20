"""Tests for the source-schema preflight.

The pure check (`check_catalogue`) is exercised against hand-built catalogues so
the comparison logic is tested without a database. The I/O wrapper (`preflight`)
reuses the fake driver/clients from test_tables so the end-to-end path -- read
the catalogue, compare, summarise -- is covered too.

The invariant that matters most: a source we could NOT read is a WARN and never a
BLOCK, because a control plane that cannot reach the customer's database is the
normal case, not a defect.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.preflight import (  # noqa: E402
    BLOCK,
    LOAD_PAUSE_FIXED_SECONDS,
    LOAD_ROWS_PER_SECOND,
    LOAD_TABLE_OVERHEAD_SECONDS,
    RETENTION_PATH,
    WARN,
    check_catalogue,
    estimate_pause,
    pause_findings,
    preflight,
    replica_identity_findings,
    keyless_findings,
    summarize,
    target_findings,
    without_path,
)
from ecdl_link.tests.test_tables import (  # noqa: E402
    Clients,
    FakeDriver,
    FakeRds,
    SECRET_ARN,
    PG_CLUSTER,
    SOURCE_ARN,
    mysql_responder,
    pg_responder,
)


def catalogue(*tables):
    """A listed catalogue from (name, [cols]) pairs."""
    return {
        'listed': True,
        'tables': [
            {'table': name,
             'columns': [{'name': c} for c in cols],
             'primary_key': [cols[0]] if cols else []}
            for name, cols in tables
        ],
    }


CAT = catalogue(
    ('catalog', ['id', 'sku', 'name', 'category', 'price', 'status']),
    ('merchants', ['merchant_id', 'name', 'country', 'risk_tier']),
)


def levels(findings):
    return [f.level for f in findings]


def paths(findings):
    return [f.path for f in findings]


class TableExistence(unittest.TestCase):

    def test_all_tables_present_is_clean(self):
        findings = check_catalogue({'tables': ['catalog', 'merchants']}, CAT)
        self.assertEqual(findings, [])

    def test_missing_table_blocks(self):
        findings = check_catalogue({'tables': ['catalog', 'customers']}, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn('customers', findings[0].message)
        self.assertEqual(findings[0].path, 'tables')

    def test_missing_table_suggests_a_close_match(self):
        # 'catalogg' is one edit from the real 'catalog'.
        findings = check_catalogue({'tables': ['catalogg']}, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn("did you mean 'catalog'", findings[0].message)

    def test_qualified_name_is_matched_by_bare_name(self):
        findings = check_catalogue({'tables': ['Inventory.catalog']}, CAT)
        self.assertEqual(findings, [])

    def test_case_insensitive_match(self):
        findings = check_catalogue({'tables': ['CATALOG']}, CAT)
        self.assertEqual(findings, [])


class ColumnReferences(unittest.TestCase):

    def _doc(self, spec):
        return {'tables': ['catalog', 'merchants'],
                'transforms': {'tables': {'catalog': spec}}}

    def test_selected_column_missing_blocks(self):
        findings = check_catalogue(self._doc({'columns': ['id', 'ghost']}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(paths(findings), ['transforms.tables.catalog.columns'])
        self.assertIn('ghost', findings[0].message)

    def test_selected_columns_all_present_is_clean(self):
        findings = check_catalogue(self._doc({'columns': ['id', 'price']}), CAT)
        self.assertEqual(findings, [])

    def test_filter_on_missing_column_blocks(self):
        findings = check_catalogue(
            self._doc({'filter': {'field': 'ghost', 'op': '=', 'value': 'x'}}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(paths(findings), ['transforms.tables.catalog.filter.field'])

    def test_filter_on_computed_field_is_allowed(self):
        # A filter may reference a field this transform creates, not only a
        # source column.
        findings = check_catalogue(self._doc({
            'computed': [{'name': 'price_cents', 'expr': 'price*100'}],
            'filter': {'field': 'price_cents', 'op': '=', 'value': '1'},
        }), CAT)
        self.assertEqual(findings, [])

    def test_rename_of_missing_column_blocks(self):
        findings = check_catalogue(self._doc({'rename': {'ghost': 'x'}}), CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertIn('ghost', findings[0].message)

    def test_rename_of_real_column_is_clean(self):
        findings = check_catalogue(self._doc({'rename': {'price': 'cost'}}), CAT)
        self.assertEqual(findings, [])

    def test_computed_expression_is_not_parsed_for_columns(self):
        # A made-up column inside an expression must NOT block: guessing wrong
        # would reject a valid config.
        findings = check_catalogue(self._doc({
            'computed': [{'name': 'x', 'expr': 'nonexistent_col * 2'}],
        }), CAT)
        self.assertEqual(findings, [])


class Denormalize(unittest.TestCase):

    def _doc(self, rule):
        return {'tables': ['catalog', 'merchants'],
                'transforms': {'tables': {'catalog': {'denormalize': [rule]}}}}

    def test_join_field_missing_on_child_blocks(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'zzz',
            'fields': ['name'],
        }), CAT)
        self.assertTrue(any(f.level == BLOCK and 'join_field' in f.path
                            for f in findings))

    def test_embedded_field_missing_on_parent_blocks(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'category',
            'fields': ['name', 'ghost'],
        }), CAT)
        self.assertTrue(any(f.level == BLOCK and 'fields' in f.path
                            for f in findings))
        self.assertTrue(any('ghost' in f.message for f in findings))

    def test_valid_denormalize_is_clean(self):
        findings = check_catalogue(self._doc({
            'parent_table': 'merchants', 'join_field': 'category',
            'fields': ['name', 'country'],
        }), CAT)
        self.assertEqual(findings, [])


class Unreachable(unittest.TestCase):

    def test_unlisted_catalogue_warns_never_blocks(self):
        cat = {'listed': False, 'error': 'no network path',
               'hint': 'type the names'}
        findings = check_catalogue({'tables': ['anything']}, cat)
        self.assertEqual(levels(findings), [WARN])
        self.assertIn('could not verify', findings[0].message)

    def test_empty_catalogue_is_a_warning(self):
        findings = check_catalogue({'tables': ['x']}, None)
        self.assertEqual(levels(findings), [WARN])

    def test_missing_table_does_not_add_column_errors(self):
        # A missing table is one block, not a block plus every column it would
        # have had.
        doc = {'tables': ['ghost'],
               'transforms': {'tables': {'ghost': {'columns': ['a', 'b', 'c']}}}}
        findings = check_catalogue(doc, CAT)
        self.assertEqual(levels(findings), [BLOCK])
        self.assertEqual(findings[0].path, 'tables')


class Summarize(unittest.TestCase):

    def test_counts_and_shape(self):
        findings = check_catalogue({'tables': ['catalog', 'nope']}, CAT)
        out = summarize(findings, checked=True)
        self.assertEqual(out['checked'], True)
        self.assertEqual(out['blocks'], 1)
        self.assertEqual(out['warnings'], 0)
        self.assertEqual(out['findings'][0]['level'], BLOCK)
        self.assertIn('message', out['findings'][0])


class PreflightIO(unittest.TestCase):
    """The wrapper that actually reads the source."""

    def _doc(self, tables, database='Inventory'):
        return {
            'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                       'secret_arn': SECRET_ARN, 'database': database},
            'tables': tables,
        }

    def _clients_driver(self):
        return Clients(rds=FakeRds()), FakeDriver(mysql_responder())

    def test_no_secret_is_a_warning_not_a_block(self):
        out = preflight({'source': {'kind': 'aws', 'arn': SOURCE_ARN},
                         'tables': ['orders']})
        self.assertFalse(out['checked'])
        self.assertEqual(out['blocks'], 0)
        self.assertEqual(out['warnings'], 1)

    def test_present_table_passes_end_to_end(self):
        clients, driver = self._clients_driver()
        out = preflight(self._doc(['orders']), clients=clients, driver=driver)
        self.assertTrue(out['checked'])
        self.assertEqual(out['blocks'], 0)

    def _gtid_driver(self, mode):
        base = mysql_responder()

        def respond(sql, params):
            if '@@gtid_mode' in sql:
                return [(mode, mode)]
            return base(sql, params)
        return FakeDriver(respond)

    def test_mysql_gtid_off_blocks_the_deploy(self):
        out = preflight(self._doc(['orders']), clients=Clients(rds=FakeRds()),
                        driver=self._gtid_driver('OFF'))
        self.assertEqual(out['blocks'], 1)
        block = [f for f in out['findings'] if f['level'] == BLOCK][0]
        self.assertIn('GTID', block['message'])

    def test_mysql_gtid_on_does_not_block(self):
        out = preflight(self._doc(['orders']), clients=Clients(rds=FakeRds()),
                        driver=self._gtid_driver('ON'))
        self.assertEqual(out['blocks'], 0)

    def test_missing_table_blocks_end_to_end(self):
        clients, driver = self._clients_driver()
        out = preflight(self._doc(['ghost_table']), clients=clients, driver=driver)
        self.assertTrue(out['checked'])
        self.assertEqual(out['blocks'], 1)
        self.assertIn('ghost_table', out['findings'][0]['message'])


class TargetEviction(unittest.TestCase):  # D6

    def levels(self, policy, **extra):
        return [f.level for f in target_findings(
            dict({'maxmemory_policy': policy, 'parameter_group': 'pg'}, **extra))]

    def test_noeviction_passes(self):
        self.assertEqual(self.levels('noeviction'), [])
        self.assertEqual(self.levels(' noeviction '), [])

    def test_every_allkeys_policy_blocks(self):
        for policy in ('allkeys-lru', 'allkeys-lfu', 'allkeys-random'):
            self.assertEqual(self.levels(policy), [BLOCK], policy)
            msg = target_findings({'maxmemory_policy': policy,
                                   'parameter_group': 'pg'})[0].message
            self.assertIn(policy, msg)
            self.assertIn('parameter group pg', msg)
            self.assertIn('noeviction', msg)

    def test_every_volatile_policy_warns(self):
        for policy in ('volatile-lru', 'volatile-lfu', 'volatile-random',
                       'volatile-ttl'):
            self.assertEqual(self.levels(policy), [WARN], policy)

    def test_an_unknown_policy_blocks(self):
        # A future policy that is neither: assume it can evict rows.
        self.assertEqual(self.levels('allkeys-something-new'), [BLOCK])
        self.assertEqual(self.levels('lru'), [BLOCK])

    def test_an_unread_policy_warns_with_the_reason(self):
        found = target_findings({'maxmemory_policy': None,
                                 'maxmemory_policy_problem': 'AccessDenied'})
        self.assertEqual([f.level for f in found], [WARN])
        self.assertIn('AccessDenied', found[0].message)
        self.assertEqual([f.level for f in target_findings({})], [WARN])
        self.assertEqual([f.level for f in target_findings(None)], [WARN])

    def test_findings_point_at_the_target(self):
        self.assertEqual({f.path for f in target_findings(
            {'maxmemory_policy': 'allkeys-lru'})}, {'target'})


class PauseAgainstRetention(unittest.TestCase):  # Jira story F056
    """The load holds capture paused; the source must keep its log that long."""

    HOUR_OF_ROWS = LOAD_ROWS_PER_SECOND * 3600

    def cat(self, rows, engine='mysql', retention=None, origin='rds', keep=None,
            prerequisites=None):
        checks = prerequisites
        if checks is None:
            checks = []
            if engine == 'mysql':
                checks.append({'name': 'binary logs retained >= 24h', 'ok': None,
                               'key': 'log_retention', 'retention_hours': retention,
                               'origin': origin})
            elif keep is not None:
                checks.append({'name': 'replication slots never invalidated',
                               'ok': keep is False, 'key': 'slot_wal_keep',
                               'slot_wal_keep_size': keep or None})
        return {'listed': True, 'engine': engine, 'prerequisites': checks,
                'tables': [{'table': name, 'row_estimate': count, 'columns': []}
                           for name, count in rows]}

    def doc(self, tables, mode='initial_load_and_cdc'):
        return {'mode': mode, 'tables': tables}

    def run_check(self, rows, mode='initial_load_and_cdc', **kw):
        return pause_findings(self.doc([n for n, _ in rows], mode), self.cat(rows, **kw))

    def fixed(self, tables):
        return LOAD_PAUSE_FIXED_SECONDS + LOAD_TABLE_OVERHEAD_SECONDS * tables

    def test_estimate_is_rows_over_rate_plus_fixed_costs(self):
        est = estimate_pause(['a', 'Inventory.b'],
                             self.cat([('a', self.HOUR_OF_ROWS), ('b', 0)]))
        self.assertEqual(est['rows'], self.HOUR_OF_ROWS)
        self.assertEqual(est['seconds'], 3600 + self.fixed(2))
        self.assertEqual(est['unknown'], [])

    def test_well_within_retention_is_clean(self):
        self.assertEqual(self.run_check([('orders', 1000)], retention=24), [])

    def test_over_half_of_retention_warns_with_the_numbers(self):
        # 3h of rows + fixed costs against 6h: over half, under all.
        found = self.run_check([('orders', 3 * self.HOUR_OF_ROWS)], retention=6)
        self.assertEqual(levels(found), [WARN])
        self.assertEqual(found[0].path, RETENTION_PATH)
        self.assertIn('3.3h', found[0].message)
        self.assertIn('(6h)', found[0].message)
        self.assertIn("rds_set_configuration('binlog retention hours', 24)",
                      found[0].message)

    def test_exactly_half_does_not_warn(self):
        rows = int((3 * 3600 - self.fixed(1)) * LOAD_ROWS_PER_SECOND)
        self.assertEqual(self.run_check([('orders', rows)], retention=6), [])

    def test_longer_than_retention_blocks_with_the_fix(self):
        found = self.run_check([('orders', 20 * self.HOUR_OF_ROWS)], retention=12)
        self.assertEqual(levels(found), [BLOCK])
        msg = found[0].message
        self.assertIn('20.3h', msg)
        self.assertIn('(12h)', msg)
        self.assertIn('lost', msg)
        # Twice the estimate, rounded up.
        self.assertIn("rds_set_configuration('binlog retention hours', 41)", msg)

    def test_self_managed_mysql_fix_is_the_server_setting(self):
        found = self.run_check([('orders', 20 * self.HOUR_OF_ROWS)], retention=12,
                               origin='server')
        self.assertIn('SET PERSIST binlog_expire_logs_seconds = {};'.format(41 * 3600),
                      found[0].message)

    def test_rds_null_retention_is_zero_and_blocks(self):
        found = self.run_check([('orders', 10)], retention=0.0)
        self.assertEqual(levels(found), [BLOCK])

    def test_unknown_retention_only_warns(self):
        found = self.run_check([('orders', 20 * self.HOUR_OF_ROWS)], retention=None)
        self.assertEqual(levels(found), [WARN])
        self.assertIn('could not be read', found[0].message)
        found = self.run_check([('orders', 10)], prerequisites=[])
        self.assertEqual(levels(found), [WARN])

    def test_unknown_row_count_only_warns(self):
        found = self.run_check([('orders', None), ('items', 10)], retention=24)
        self.assertEqual(levels(found), [WARN])
        self.assertIn('orders', found[0].message)
        self.assertIn('ANALYZE', found[0].message)

    def test_known_rows_alone_can_still_block(self):
        # A lower bound that already outlasts the log is enough to know.
        found = self.run_check([('orders', None), ('items', 20 * self.HOUR_OF_ROWS)],
                               retention=12)
        self.assertEqual(sorted(levels(found)), [BLOCK, WARN])
        block = [f for f in found if f.level == BLOCK][0]
        self.assertIn('at least', block.message)

    def test_tables_are_summed_and_each_costs_its_overhead(self):
        # Two tables of 6h each: 12h + fixed > 12h.
        found = self.run_check([('a', 6 * self.HOUR_OF_ROWS), ('b', 6 * self.HOUR_OF_ROWS)],
                               retention=12)
        self.assertEqual(levels(found), [BLOCK])

    def test_only_the_mode_that_pauses_is_checked(self):
        for mode in ('cdc', 'initial_load', None):
            self.assertEqual(self.run_check([('orders', 20 * self.HOUR_OF_ROWS)],
                                            mode=mode, retention=1), [], mode)

    def test_an_unread_catalogue_adds_nothing(self):
        self.assertEqual(pause_findings(self.doc(['orders']), {'listed': False}), [])

    def test_postgres_with_unlimited_slots_is_clean(self):
        self.assertEqual(self.run_check([('orders', 20 * self.HOUR_OF_ROWS)],
                                        engine='postgresql', keep=None), [])

    def test_postgres_finite_slot_wal_limit_warns_with_the_estimate(self):
        found = self.run_check([('orders', 20 * self.HOUR_OF_ROWS)],
                               engine='postgresql', keep='10GB')
        self.assertEqual(levels(found), [WARN])
        self.assertIn('max_slot_wal_keep_size=10GB', found[0].message)
        self.assertIn('20.3h', found[0].message)

    def test_without_path_drops_and_recounts(self):
        summary = summarize([check_catalogue({'tables': ['ghost']}, CAT)[0]]
                            + self.run_check([('orders', 20 * self.HOUR_OF_ROWS)],
                                             retention=1)
                            + self.run_check([('orders', None)], retention=24), True)
        self.assertEqual((summary['blocks'], summary['warnings']), (2, 1))
        left = without_path(summary, RETENTION_PATH)
        self.assertEqual((left['blocks'], left['warnings']), (1, 0))
        self.assertEqual([f['path'] for f in left['findings']], ['tables'])


class PauseEndToEnd(unittest.TestCase):  # Jira story F056, through list_tables
    def test_short_rds_retention_blocks_a_large_load(self):
        def respond(sql, params):
            if 'information_schema.TABLES' in sql:
                return [('orders', 30 * LOAD_ROWS_PER_SECOND * 3600)]
            if 'rds_show_configuration' in sql:
                return [('binlog retention hours', '12', '')]
            return mysql_responder()(sql, params)
        out = preflight({'mode': 'initial_load_and_cdc',
                         'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                                    'secret_arn': SECRET_ARN, 'database': 'Inventory'},
                         'tables': ['orders']},
                        clients=Clients(rds=FakeRds()), driver=FakeDriver(respond))
        blocks = [f for f in out['findings'] if f['level'] == BLOCK]
        self.assertEqual([f['path'] for f in blocks], [RETENTION_PATH])


def pg_catalogue(*tables, **kw):
    """A listed PostgreSQL catalogue from (name, identity, has_pk, toastable)."""
    return {
        'listed': True, 'engine': 'postgresql',
        'namespace': kw.get('namespace', 'public'),
        'tables': [{'table': name, 'columns': [{'name': 'id'}],
                    'primary_key': ['id'] if pk else [],
                    'replica_identity': ident, 'toastable_columns': list(toast)}
                   for name, ident, pk, toast in tables],
    }


class ReplicaIdentity(unittest.TestCase):  # Jira story F035

    def check(self, *tables, **kw):
        names = kw.get('doc_tables') or [t[0] for t in tables]
        return replica_identity_findings({'tables': names}, pg_catalogue(*tables))

    def test_full_passes_even_with_toastable_columns(self):
        self.assertEqual(self.check(('orders', 'f', True, ['body'])), [])

    def test_default_with_a_primary_key_and_no_toast_passes(self):
        self.assertEqual(self.check(('orders', 'd', True, [])), [])

    def test_nothing_blocks_with_the_fix(self):
        out = self.check(('orders', 'n', True, []))
        self.assertEqual(levels(out), [BLOCK])
        self.assertIn('REPLICA IDENTITY NOTHING', out[0].message)
        self.assertIn('ALTER TABLE public.orders REPLICA IDENTITY FULL;', out[0].message)
        self.assertIn('refuses every UPDATE and DELETE', out[0].message)

    def test_default_without_a_primary_key_blocks(self):
        out = self.check(('orders', 'd', False, []))
        self.assertEqual(levels(out), [BLOCK])
        self.assertIn('no primary key', out[0].message)
        self.assertIn('REPLICA IDENTITY FULL;', out[0].message)

    def test_default_with_toastable_columns_warns_with_the_fix(self):
        out = self.check(('docs', 'd', True, ['body', 'img']))
        self.assertEqual(levels(out), [WARN])
        self.assertIn('body, img', out[0].message)
        self.assertIn('ALTER TABLE public.docs REPLICA IDENTITY FULL;', out[0].message)

    def test_using_index_is_acceptable_and_warns_on_toast(self):
        self.assertEqual(self.check(('orders', 'i', False, [])), [])
        self.assertEqual(levels(self.check(('docs', 'i', False, ['body']))), [WARN])

    def test_only_link_tables_are_checked(self):
        out = self.check(('orders', 'f', True, []), ('other', 'n', True, []),
                         doc_tables=['orders'])
        self.assertEqual(out, [])

    def test_qualified_names_match_and_other_schemas_are_skipped(self):
        self.assertEqual(levels(self.check(('orders', 'n', True, []),
                                           doc_tables=['public.orders'])), [BLOCK])
        self.assertEqual(self.check(('orders', 'n', True, []),
                                    doc_tables=['sales.orders']), [])

    def test_unread_identity_is_not_reported(self):
        cat = pg_catalogue(('orders', None, False, []))
        self.assertEqual(replica_identity_findings({'tables': ['orders']}, cat), [])

    def test_other_engines_and_unlisted_catalogues_are_ignored(self):
        cat = dict(pg_catalogue(('orders', 'n', True, [])), engine='mysql')
        self.assertEqual(replica_identity_findings({'tables': ['orders']}, cat), [])
        cat = dict(pg_catalogue(('orders', 'n', True, [])), listed=False)
        self.assertEqual(replica_identity_findings({'tables': ['orders']}, cat), [])

    def test_a_mixed_case_name_is_quoted_in_the_fix(self):
        out = self.check(('Orders', 'n', True, []), doc_tables=['Orders'])
        self.assertIn('ALTER TABLE public."Orders" REPLICA IDENTITY FULL;',
                      out[0].message)

    def test_end_to_end_through_the_driver(self):
        driver = FakeDriver(pg_responder(identity=[('orders', 'n', False)]))
        out = preflight({'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                                    'secret_arn': SECRET_ARN, 'database': 'shop'},
                         'tables': ['orders']},
                        clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=driver)
        self.assertTrue(out['checked'])
        self.assertEqual(out['blocks'], 1)
        self.assertIn('REPLICA IDENTITY NOTHING', ' '.join(
            f['message'] for f in out['findings']))


if __name__ == '__main__':
    unittest.main()


class Keyless(unittest.TestCase):  # Jira story F025
    def cat(self, *tables, **kw):
        """(name, columns, primary key) triples."""
        return {'listed': True, 'engine': kw.get('engine', 'mysql'), 'namespace': 'shop',
                'tables': [dict({'table': n, 'columns': [{'name': c} for c in cols],
                                 'primary_key': pk}, **kw.get('extra', {}))
                           for n, cols, pk in tables]}

    def doc(self, *names, mode='cdc'):
        return {'mode': mode, 'tables': list(names)}

    def test_no_key_and_no_id_blocks(self):
        out = keyless_findings(self.doc('events'), self.cat(('events', ['ts', 'msg'], [])))
        self.assertEqual(levels(out), [BLOCK])
        self.assertIn("table 'events' has no primary key and no 'id' column", out[0].message)
        self.assertIn('dead-letter every change', out[0].message)

    def test_a_primary_key_or_an_id_column_passes(self):
        cat = self.cat(('orders', ['order_no', 'x'], ['order_no']), ('logs', ['ID', 'x'], []))
        self.assertEqual(keyless_findings(self.doc('orders', 'logs'), cat), [])

    def test_only_streaming_modes_and_link_tables(self):
        cat = self.cat(('events', ['ts'], []), ('other', ['ts'], []))
        self.assertEqual(keyless_findings(self.doc('events', mode='initial_load'), cat), [])
        self.assertEqual(levels(keyless_findings(self.doc('events', mode='initial_load_and_cdc'), cat)),
                         [BLOCK])
        self.assertEqual(levels(keyless_findings(self.doc('events'), cat)), [BLOCK])

    def test_unread_key_or_unlisted_catalogue_is_not_reported(self):
        cat = self.cat(('events', ['ts'], []))
        del cat['tables'][0]['primary_key']
        self.assertEqual(keyless_findings(self.doc('events'), cat), [])
        self.assertEqual(keyless_findings(self.doc('events'),
                                          dict(self.cat(('events', ['ts'], [])), listed=False)), [])

    def test_postgresql_identity_block_is_not_repeated(self):
        for ident, want in (('d', []), ('n', []), ('f', [BLOCK])):
            cat = self.cat(('events', ['ts'], []), engine='postgresql',
                           extra={'replica_identity': ident})
            self.assertEqual(levels(keyless_findings(self.doc('events'), cat)), want, ident)

    def test_preflight_reports_it(self):
        respond = mysql_responder(tables=[('events', 10)],
                                  columns=[('events', 'ts', 'datetime', 'NO', ''),
                                           ('events', 'msg', 'varchar', 'YES', '')])
        out = preflight({'mode': 'cdc',
                         'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                                    'secret_arn': SECRET_ARN, 'database': 'Inventory'},
                         'tables': ['events']},
                        clients=Clients(rds=FakeRds()), driver=FakeDriver(respond))
        blocks = [f['message'] for f in out['findings'] if f['level'] == BLOCK]
        self.assertTrue(any("has no primary key and no 'id' column" in m for m in blocks), blocks)
