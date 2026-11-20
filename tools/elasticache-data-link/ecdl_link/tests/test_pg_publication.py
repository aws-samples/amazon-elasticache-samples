"""Jira story F007: PostgreSQL publications, the signalling table, and whether a snapshot
signal was read.

PostgreSQL sends Debezium only the tables in the publication it reads, so a
link table or the signalling table missing from it is silently never captured,
and every backfill signal is silently ignored. Covered here:

  * the per-link publication name, how it reaches Debezium, and that a running
    link keeps the publication it was deployed with
  * the SQL `ecdl signal ddl` prints and the preflight's checks against the
    catalogue
  * an added table's ALTER PUBLICATION as a step of the update review
  * the broker watch that reports whether Debezium read a snapshot signal,
    against fakes and (with ECDL_TEST_VALKEY) a real Valkey
"""
import argparse
import contextlib
import copy
import io
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link import change, signals  # noqa: E402
from ecdl_link.preflight import BLOCK, WARN, preflight, publication_findings  # noqa: E402
from ecdl_link.provision import (  # noqa: E402
    LEGACY_PG_PUBLICATION, deployed_pg_publication, pg_publication_in_use,
    pg_publication_name, plan,
)
from ecdl_link.tests.test_change import cdc_params, plan_with  # noqa: E402
from ecdl_link.tests.test_deploy import FakeCloudFormation, FakeECS, deployer  # noqa: E402
from ecdl_link.tests.test_provision import resolution  # noqa: E402
from ecdl_link.tests.test_tables import (  # noqa: E402
    PG_CLUSTER, SECRET_ARN, SOURCE_ARN, Clients, FakeDriver, FakeRds, pg_responder,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pg_cdc_step():
    r = copy.deepcopy(resolution('cdc'))
    r['source']['engine'] = 'postgresql'
    return next(s for s in plan(r)['steps'] if s['id'] == 'cdc')


# ── (1) the publication name and how it reaches Debezium ───────────────────


class PublicationName(unittest.TestCase):

    def test_derived_like_the_slot(self):
        self.assertEqual(pg_publication_name('checkout'), 'ecdl_checkout_pub')
        self.assertEqual(pg_publication_name('Shop-EU.v2'), 'ecdl_shop_eu_v2_pub')

    def test_a_long_name_keeps_the_suffix_within_63(self):
        name = pg_publication_name('x' * 100)
        self.assertEqual(len(name), 63)
        self.assertTrue(name.endswith('_pub'))

    def test_matches_the_template_pattern(self):
        import re
        for link in ('a', 'Shop-EU', 'x' * 80, 'üñí'):
            self.assertRegex(pg_publication_name(link), re.compile(r'^[a-z0-9_]{0,63}$'))

    def test_postgres_cdc_sends_it_and_others_do_not(self):
        self.assertEqual(pg_cdc_step()['parameters']['PgPublicationName'],
                         'ecdl_checkout_pub')
        mysql = next(s for s in plan(resolution('cdc'))['steps'] if s['id'] == 'cdc')
        self.assertNotIn('PgPublicationName', mysql['parameters'])

    def test_it_is_kept_on_update(self):
        self.assertIn('PgPublicationName', pg_cdc_step()['keep_previous'])
        self.assertIn('PgPublicationName', change.KEPT)


class PublicationKeptOnUpdate(unittest.TestCase):

    def _update(self, existing_params):
        class Cfn(FakeCloudFormation):
            def describe_stacks(self, StackName=None, **kw):
                out = super(Cfn, self).describe_stacks(StackName=StackName, **kw)
                out['Stacks'][0]['Parameters'] = [
                    {'ParameterKey': k, 'ParameterValue': v}
                    for k, v in existing_params.items()]
                return out
        step = dict(pg_cdc_step(), from_outputs={})
        cfn = Cfn(existing={step['name']: 'CREATE_COMPLETE'},
                  states={step['name']: ['CREATE_COMPLETE', 'UPDATE_COMPLETE']})
        deployer(cfn, FakeECS())._stack_step(step, {})
        sent = {p['ParameterKey']: p for p in cfn.updated[0]['parameters']}
        return sent['PgPublicationName']

    def test_a_per_link_publication_stays(self):
        self.assertEqual(self._update({'PgPublicationName': 'ecdl_checkout_pub'}),
                         {'ParameterKey': 'PgPublicationName', 'UsePreviousValue': True})

    def test_a_stack_from_before_per_link_publications_keeps_dbz_pub(self):
        # Empty -> the image default dbz_pub, which that stack reads.
        self.assertEqual(self._update({'PgSlotName': 'ecdl_checkout'}),
                         {'ParameterKey': 'PgPublicationName', 'ParameterValue': ''})

    def test_review_does_not_report_it_as_a_change(self):
        r = change.review_update(
            plan_with(PgPublicationName='ecdl_l_pub', SourceEngine='postgresql'),
            {'name': 'l', 'mode': 'cdc'},
            {'cdc': cdc_params(SourceEngine='postgresql')}, {}, {})
        self.assertNotIn('PgPublicationName', [d['parameter'] for d in r['drift']])


class PublicationInUse(unittest.TestCase):

    def test_new_link_gets_its_own(self):
        self.assertEqual(pg_publication_in_use('shop'), 'ecdl_shop_pub')

    def test_deployed_link_keeps_what_it_runs_with(self):
        self.assertEqual(pg_publication_in_use('shop', {'PgPublicationName': 'ecdl_shop_pub'}),
                         'ecdl_shop_pub')
        self.assertEqual(pg_publication_in_use('shop', {'PgPublicationName': ''}), 'dbz_pub')
        self.assertEqual(pg_publication_in_use('shop', {}), LEGACY_PG_PUBLICATION)

    def test_from_the_live_stack(self):
        class Cfn(object):
            def __init__(self, params=None, missing=False, error=None):
                self.params, self.missing, self.error = params, missing, error

            def describe_stacks(self, StackName=None):
                if self.error:
                    raise RuntimeError(self.error)
                if self.missing:
                    raise RuntimeError('Stack with id {} does not exist'.format(StackName))
                return {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'Parameters': [
                    {'ParameterKey': k, 'ParameterValue': v} for k, v in self.params.items()]}]}
        self.assertEqual(deployed_pg_publication(Cfn(missing=True), 'shop'), 'ecdl_shop_pub')
        self.assertEqual(deployed_pg_publication(Cfn({'PgSlotName': ''}), 'shop'), 'dbz_pub')
        self.assertEqual(deployed_pg_publication(
            Cfn({'PgPublicationName': 'ecdl_shop_pub'}), 'shop'), 'ecdl_shop_pub')
        with self.assertRaises(RuntimeError):
            deployed_pg_publication(Cfn(error='AccessDenied'), 'shop')


class TemplateAndImage(unittest.TestCase):

    def read(self, *parts):
        with open(os.path.join(ROOT, *parts)) as handle:
            return handle.read()

    def test_template_passes_it_as_publication_only_when_set(self):
        src = self.read('infra', 'ecdl-cdc-ecs.yaml')
        self.assertIn("  PgPublicationName:\n    Type: String\n    Default: ''\n"
                      "    AllowedPattern: '^[a-z0-9_]{0,63}$'", src)
        self.assertIn("HasPgPublicationName: !Not [!Equals [!Ref PgPublicationName, '']]", src)
        self.assertIn('- HasPgPublicationName\n'
                      '              - { Name: PUBLICATION, Value: !Ref PgPublicationName }',
                      src)

    def test_image_reads_it_with_the_legacy_default(self):
        self.assertIn('debezium.source.publication.name=${PUBLICATION:dbz_pub}\n',
                      self.read('data-plane', 'debezium-server',
                                'application-postgresql.properties'))
        self.assertIn('debezium.source.publication.autocreate.mode=disabled',
                      self.read('data-plane', 'debezium-server',
                                'application-postgresql.properties'))

    def test_entrypoint_logs_slot_and_publication(self):
        self.assertIn("publication '${PUBLICATION:-dbz_pub}'",
                      self.read('data-plane', 'debezium-server', 'entrypoint.sh'))


# ── (2) the SQL, and the preflight ─────────────────────────────────────────


class PublicationSql(unittest.TestCase):

    def test_create_and_alter(self):
        self.assertEqual(signals.pg_publication_sql('p', ['public.a', 'public.b'], create=True),
                         'CREATE PUBLICATION p FOR TABLE public.a, public.b;')
        self.assertEqual(signals.pg_publication_sql('p', ['public.a']),
                         'ALTER PUBLICATION p ADD TABLE public.a;')

    def test_quoted_names_pass_and_injection_is_refused(self):
        self.assertIn('public."Orders"', signals.pg_publication_sql('p', ['public."Orders"']))
        for bad in ('public.a; DROP TABLE b', 'a', 'public.a b', 'x.y.z', '"a"b.c'):
            with self.assertRaises(signals.SignalError, msg=bad):
                signals.pg_publication_sql('p', [bad])
        with self.assertRaises(signals.SignalError):
            signals.pg_publication_sql('p', [])
        with self.assertRaises(Exception):
            signals.pg_publication_sql('p; DROP', ['public.a'])

    def test_grant(self):
        self.assertEqual(signals.pg_signal_grant_sql('public.ecdl_signal_x', 'cdc'),
                         'GRANT INSERT ON public.ecdl_signal_x TO cdc;')
        self.assertIn('<the CDC user>', signals.pg_signal_grant_sql('public.s', 'a b'))
        self.assertIn('<the CDC user>', signals.pg_signal_grant_sql('public.s', None))

    def test_ddl_names_every_table_and_the_signalling_table(self):
        sql = signals.pg_prerequisite_sql('ecdl_x_pub', ['public.orders', 'public.items'],
                                          'public.ecdl_signal_x', 'cdc')
        self.assertIn('CREATE PUBLICATION ecdl_x_pub FOR TABLE public.orders, '
                      'public.items, public.ecdl_signal_x;', sql)
        self.assertIn('-- ALTER PUBLICATION ecdl_x_pub ADD TABLE public.orders, '
                      'public.items, public.ecdl_signal_x;', sql)
        self.assertIn('GRANT INSERT ON public.ecdl_signal_x TO cdc;', sql)
        # Every line is SQL or a comment, so the whole output can be run.
        for line in sql.splitlines():
            self.assertTrue(line.startswith('--') or line.endswith(';'), line)


DOC = {'name': 'shop', 'source': {'database': 'shop'}, 'tables': ['orders', 'items']}
SIGNAL = 'public.ecdl_signal_shop'


def catalogue(tables=('orders', 'items', 'ecdl_signal_shop'), publications=None,
              insertable=True, user='cdc'):
    entries = [{'table': t, 'columns': [{'name': 'id'}], 'primary_key': ['id'],
                'insertable': insertable if t.startswith('ecdl_signal') else False}
               for t in tables]
    return {'listed': True, 'engine': 'postgresql', 'namespace': 'public',
            'tables': entries, 'publications': publications, 'user': user}


FULL = {'ecdl_shop_pub': ['public.orders', 'public.items', SIGNAL]}


def messages(findings):
    return ' | '.join(f.message for f in findings)


class PublicationPreflight(unittest.TestCase):

    def check(self, cat, publication='ecdl_shop_pub', doc=DOC):
        return publication_findings(doc, cat, publication)

    def test_complete_publication_passes(self):
        self.assertEqual(self.check(catalogue(publications=FULL)), [])

    def test_missing_link_table_blocks_with_the_alter(self):
        out = self.check(catalogue(publications={'ecdl_shop_pub': ['public.orders', SIGNAL]}))
        self.assertEqual([f.level for f in out], [BLOCK])
        self.assertIn('ALTER PUBLICATION ecdl_shop_pub ADD TABLE public.items;', out[0].message)
        self.assertIn('never captured', out[0].message)
        self.assertNotIn('backfill', out[0].message)

    def test_missing_signalling_table_blocks_and_says_backfills_are_ignored(self):
        out = self.check(catalogue(publications={'ecdl_shop_pub': ['public.orders',
                                                                  'public.items']}))
        self.assertEqual([f.level for f in out], [BLOCK])
        self.assertIn('ADD TABLE {};'.format(SIGNAL), out[0].message)
        self.assertIn('silently ignored', out[0].message)

    def test_missing_publication_blocks_with_the_create(self):
        out = self.check(catalogue(publications={'dbz_pub': ['public.orders']}))
        self.assertEqual([f.level for f in out], [BLOCK])
        self.assertIn('CREATE PUBLICATION ecdl_shop_pub FOR TABLE public.orders, '
                      'public.items, {};'.format(SIGNAL), out[0].message)

    def test_a_legacy_link_is_checked_against_dbz_pub(self):
        cat = catalogue(publications={'dbz_pub': ['public.orders', 'public.items', SIGNAL]})
        self.assertEqual(self.check(cat, publication='dbz_pub'), [])
        self.assertEqual([f.level for f in self.check(cat)], [BLOCK])

    def test_unknown_publication_is_a_warning(self):
        out = self.check(catalogue(publications={}), publication=None)
        self.assertEqual([f.level for f in out], [WARN])
        self.assertIn('ecdl_shop_pub', out[0].message)
        self.assertIn('dbz_pub', out[0].message)

    def test_unreadable_publications_warn(self):
        out = self.check(catalogue(publications=None))
        self.assertEqual([f.level for f in out], [WARN])
        self.assertIn('could not read the publications', out[0].message)

    def test_for_all_tables_publication_passes(self):
        # pg_publication_tables lists every table of a FOR ALL TABLES publication.
        cat = catalogue(publications={'ecdl_shop_pub': ['public.orders', 'public.items',
                                                        SIGNAL, 'public.other']})
        self.assertEqual(self.check(cat), [])

    def test_case_is_ignored_when_comparing_members(self):
        cat = catalogue(publications={'ecdl_shop_pub': ['public.ORDERS', 'public.items',
                                                        SIGNAL]})
        self.assertEqual(self.check(cat), [])

    def test_missing_signalling_table_blocks(self):
        out = self.check(catalogue(tables=('orders', 'items'), publications=FULL))
        self.assertEqual([f.level for f in out], [BLOCK])
        self.assertIn('does not exist', out[0].message)
        self.assertIn('ecdl signal ddl', out[0].message)

    def test_no_insert_on_the_signalling_table_warns_with_the_grant(self):
        out = self.check(catalogue(publications=FULL, insertable=False))
        self.assertEqual([f.level for f in out], [WARN])
        self.assertIn('GRANT INSERT ON {} TO cdc;'.format(SIGNAL), out[0].message)

    def test_a_table_missing_from_the_source_is_left_to_the_table_check(self):
        out = self.check(catalogue(tables=('orders', 'ecdl_signal_shop'),
                                   publications={'ecdl_shop_pub': ['public.orders', SIGNAL]}))
        self.assertEqual(out, [])

    def test_a_table_in_another_schema_is_checked_by_name(self):
        doc = dict(DOC, tables=['orders', 'sales.items'])
        out = self.check(catalogue(publications=FULL), doc=doc)
        self.assertIn('ADD TABLE sales.items;', messages(out))

    def test_signal_collection_override_is_used(self):
        doc = dict(DOC, advanced={'debezium': {'signal_collection': 'public.my_signals'}})
        out = self.check(catalogue(tables=('orders', 'items', 'my_signals'),
                                   publications=FULL), doc=doc)
        self.assertIn('public.my_signals', messages(out))

    def test_other_engines_and_unnamed_documents_are_ignored(self):
        self.assertEqual(self.check(dict(catalogue(publications={}), engine='mysql')), [])
        self.assertEqual(self.check(catalogue(publications={}),
                                    doc={'tables': ['orders']}), [])

    def test_end_to_end_through_the_driver(self):
        base = pg_responder(tables=[('orders', 1), ('ecdl_signal_shop', 0)],
                            identity=[('orders', 'f', False), ('ecdl_signal_shop', 'd', True)])

        def respond(sql, params):
            if 'pg_publication_tables' in sql:
                return [('ecdl_shop_pub', 'public', 'orders')]
            if 'pg_publication' in sql:
                return [('ecdl_shop_pub',), ('dbz_pub',)]
            return base(sql, params)
        doc = {'name': 'shop', 'source': {'kind': 'aws', 'arn': SOURCE_ARN,
                                          'secret_arn': SECRET_ARN, 'database': 'shop'},
               'tables': ['orders']}
        out = preflight(doc, clients=Clients(rds=FakeRds(PG_CLUSTER)),
                        driver=FakeDriver(respond), publication='ecdl_shop_pub')
        self.assertEqual(out['blocks'], 1, out)
        self.assertIn('ALTER PUBLICATION ecdl_shop_pub ADD TABLE {};'.format(SIGNAL),
                      ' '.join(f['message'] for f in out['findings']))
        # Unknown publication: the same gap is only a warning.
        out = preflight(doc, clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=FakeDriver(respond))
        self.assertEqual(out['blocks'], 0, out)


class PublicationCatalogue(unittest.TestCase):

    def test_publications_and_user_are_listed(self):
        from ecdl_link.tables import list_tables
        base = pg_responder()

        def respond(sql, params):
            if 'pg_publication_tables' in sql:
                return [('p1', 'public', 'orders'), ('p1', 'sales', 'x')]
            if 'pg_publication' in sql:
                return [('p1',), ('empty',)]
            return base(sql, params)
        out = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                          clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=FakeDriver(respond))
        self.assertEqual(out['publications'], {'p1': ['public.orders', 'sales.x'], 'empty': []})
        self.assertEqual(out['user'], 'postgres')

    def test_unreadable_publications_are_none(self):
        from ecdl_link.tables import list_tables
        base = pg_responder()

        def respond(sql, params):
            if 'pg_publication' in sql:
                raise RuntimeError('permission denied')
            return base(sql, params)
        out = list_tables(SOURCE_ARN, SECRET_ARN, database='shop',
                          clients=Clients(rds=FakeRds(PG_CLUSTER)), driver=FakeDriver(respond))
        self.assertTrue(out['listed'])
        self.assertIsNone(out['publications'])


# ── (3) an added table in an update ────────────────────────────────────────


def pg_resolution(tables=('public.orders', 'public.items')):
    return {'name': 'l', 'source': {'engine': 'postgresql'},
            'tables': [{'input': t, 'qualified': t} for t in tables],
            'debezium': {'signal_collection': 'public.ecdl_signal_l'}}


class AddedTableReview(unittest.TestCase):

    def review(self, engine='postgresql', deployed_pub=''):
        deployed = {'cdc': cdc_params(SourceEngine=engine, Tables='public.orders',
                                      PgPublicationName=deployed_pub, TransformsJson='')}
        return change.review_update(
            plan_with(SourceEngine=engine, Tables='public.orders,public.items',
                      TransformsJson=''),
            {'name': 'l', 'mode': 'cdc'}, deployed, {}, {})

    def test_the_alter_publication_is_a_required_step_before_the_snapshot(self):
        r = self.review(deployed_pub='ecdl_l_pub')
        self.assertEqual(r['pg_publication'], 'ecdl_l_pub')
        plan_doc, outstanding = change.follow_up(r, pg_resolution())
        kinds = [s['kind'] for s in plan_doc['steps']]
        self.assertEqual(kinds[0], 'source_sql')
        self.assertLess(kinds.index('source_sql'), kinds.index('signal'))
        self.assertEqual(outstanding[0]['kind'], 'source_sql')
        self.assertEqual(outstanding[0]['statement'],
                         'ALTER PUBLICATION ecdl_l_pub ADD TABLE public.items;')
        self.assertTrue(r['needs_ack'])

    def test_a_legacy_link_alters_dbz_pub(self):
        _plan, outstanding = change.follow_up(self.review(deployed_pub=''), pg_resolution())
        self.assertEqual(outstanding[0]['statement'],
                         'ALTER PUBLICATION dbz_pub ADD TABLE public.items;')

    def test_other_engines_have_no_publication_step(self):
        r = self.review(engine='mysql')
        self.assertNotIn('pg_publication', r)
        res = dict(pg_resolution(), source={'engine': 'mysql'})
        plan_doc, _ = change.follow_up(r, res)
        self.assertNotIn('source_sql', [s['kind'] for s in plan_doc['steps']])

    def test_without_a_known_publication_a_note_says_what_to_do(self):
        from ecdl_link.repair import describe_repair, diff, repair_plan
        d = diff({'name': 'l', 'mode': 'cdc', 'source': {'arn': 'a'}, 'target': {'arn': 't'},
                  'tables': ['public.orders']},
                 {'name': 'l', 'mode': 'cdc', 'source': {'arn': 'a'}, 'target': {'arn': 't'},
                  'tables': ['public.orders', 'public.items']})
        out = repair_plan(d, pg_resolution())
        self.assertIn('ALTER PUBLICATION', ' '.join(out['notes']))
        self.assertNotIn('source_sql', [s['kind'] for s in out['steps']])
        with_pub = repair_plan(d, pg_resolution(), pg_publication='ecdl_l_pub')
        self.assertIn('run on the source: ALTER PUBLICATION ecdl_l_pub ADD TABLE '
                      'public.items;', describe_repair(with_pub))

    def test_no_added_table_no_step(self):
        deployed = {'cdc': cdc_params(SourceEngine='postgresql', Tables='public.orders',
                                      TransformsJson='')}
        r = change.review_update(plan_with(SourceEngine='postgresql', Tables='public.orders',
                                           TransformsJson=''),
                                 {'name': 'l', 'mode': 'cdc'}, deployed, {}, {})
        plan_doc, outstanding = change.follow_up(r, pg_resolution(('public.orders',)))
        self.assertEqual(outstanding, [])


# ── (4) did Debezium read the signal? ──────────────────────────────────────


class FakeBroker(object):
    """The calls wait_for_snapshot makes: hgetall, xinfo_stream, xrange."""

    def __init__(self):
        self.offsets = {}
        self.streams = {}
        self.script = []   # callables run on each hgetall: Debezium's progress

    def hgetall(self, key):
        if self.script:
            self.script.pop(0)(self)
        return dict(self.offsets.get(key) or {})

    def xinfo_stream(self, stream):
        if stream not in self.streams:
            raise RuntimeError('ERR no such key')
        entries = self.streams[stream]
        return {'last-generated-id': entries[-1][0] if entries else '0-0'}

    def xrange(self, stream, min='-', max='+', count=None):
        after = min[1:] if min.startswith('(') else None
        out = [e for e in self.streams.get(stream, [])
               if after is None or _id(e[0]) > _id(after)]
        return out[:count]

    def add(self, stream, entry_id, op):
        self.streams.setdefault(stream, []).append(
            (entry_id, {'value': json.dumps({'op': op})}))


def _id(text):
    ms, _, seq = text.partition('-')
    return int(ms), int(seq or 0)


def offset_with(collections=None, correlation=None):
    value = {'lsn': 1}
    if collections:
        value['incremental_snapshot_collections'] = json.dumps(
            [{'incremental_snapshot_collections_id': c,
              'incremental_snapshot_collections_additional_condition': None}
             for c in collections])
        value['incremental_snapshot_primary_key'] = 'aced0005'
    if correlation:
        value['incremental_snapshot_correlation_id'] = correlation
    return {'["redis",{"server":"l"}]': json.dumps(value)}


class Clock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


STREAM = 'data:l.public.items'
KEY = 'ecdl:l:offsets'


class WaitForSnapshot(unittest.TestCase):

    def run_wait(self, broker, timeout=20, signal_id='execute-snapshot-1'):
        clock = Clock()
        baseline = signals.stream_positions(broker, [STREAM])
        return signals.wait_for_snapshot(broker, signal_id, ['public.items'], [STREAM],
                                         KEY, baseline, timeout=timeout, interval=1,
                                         settle=3, clock=clock, sleep=clock.sleep)

    def test_running_then_cleared_is_finished(self):
        b = FakeBroker()
        b.add(STREAM, '5-0', 'u')
        b.offsets[KEY] = offset_with()
        running = offset_with(['public.items'], 'execute-snapshot-1')
        done = offset_with()
        b.script = [lambda s: None, lambda s: None,
                    lambda s: s.offsets.__setitem__(KEY, running),
                    lambda s: s.add(STREAM, '9-0', 'r'),
                    lambda s: None,
                    lambda s: s.offsets.__setitem__(KEY, done)]
        out = self.run_wait(b)
        self.assertEqual(out['status'], 'finished', out)
        self.assertIn("Debezium's offset lists an incremental snapshot of public.items",
                      out['evidence'])

    def test_still_running_at_the_timeout_is_consumed(self):
        b = FakeBroker()
        b.offsets[KEY] = offset_with(['public.items'], 'execute-snapshot-1')
        out = self.run_wait(b, timeout=5)
        self.assertEqual(out['status'], 'consumed', out)
        self.assertIn('still running', out['detail'])

    def test_snapshot_rows_alone_count_as_consumed(self):
        b = FakeBroker()
        b.add(STREAM, '5-0', 'u')
        b.offsets[KEY] = offset_with()
        b.script = [lambda s: None, lambda s: s.add(STREAM, '6-0', 'r')]
        out = self.run_wait(b)
        # Rows seen and the offset never listed the table: finished once the
        # settle time has passed with nothing running.
        self.assertEqual(out['status'], 'finished', out)
        self.assertIn('snapshot rows (op "r") arrived on ' + STREAM, out['evidence'])

    def test_rows_before_the_offset_commit_are_not_taken_as_finished(self):
        # Rows reach the stream up to a second before Debezium commits the
        # offset that lists the snapshot; "finished" must wait for that.
        b = FakeBroker()
        b.add(STREAM, '5-0', 'u')
        b.offsets[KEY] = offset_with()
        running = offset_with(['public.items'], 'execute-snapshot-1')
        b.script = [lambda s: None, lambda s: s.add(STREAM, '6-0', 'r'),
                    lambda s: s.offsets.__setitem__(KEY, running)]
        self.assertEqual(self.run_wait(b, timeout=8)['status'], 'consumed')

    def test_rows_from_before_the_signal_do_not_count(self):
        b = FakeBroker()
        b.add(STREAM, '5-0', 'r')
        b.offsets[KEY] = offset_with()
        out = self.run_wait(b, timeout=4)
        self.assertEqual(out['status'], 'unverified', out)
        self.assertIn('no new entries', out['detail'])

    def test_ordinary_changes_are_not_mistaken_for_the_snapshot(self):
        b = FakeBroker()
        b.add(STREAM, '5-0', 'u')
        b.offsets[KEY] = offset_with()
        b.script = [lambda s: s.add(STREAM, '6-0', 'u'), lambda s: s.add(STREAM, '7-0', 'c')]
        out = self.run_wait(b, timeout=4)
        self.assertEqual(out['status'], 'unverified', out)
        self.assertIn('received entries after the signal, but no snapshot row', out['detail'])
        self.assertIn('publication', out['detail'])

    def test_a_snapshot_already_running_needs_the_signal_id(self):
        b = FakeBroker()
        b.offsets[KEY] = offset_with(['public.items'], 'an-older-signal')
        out = self.run_wait(b, timeout=3)
        self.assertEqual(out['status'], 'unverified', out)
        self.assertIn('already running', out['detail'])
        b.script = [lambda s: s.offsets.__setitem__(
            KEY, offset_with(['public.items'], 'execute-snapshot-1'))] * 2
        self.assertEqual(self.run_wait(b, timeout=3)['status'], 'consumed')

    def test_unreadable_offset_and_missing_stream(self):
        class Broken(FakeBroker):
            def hgetall(self, key):
                raise RuntimeError('NOAUTH')
        b = Broken()
        self.assertEqual(signals.stream_positions(b, [STREAM]), {STREAM: None})
        out = self.run_wait(b, timeout=2)
        self.assertEqual(out['status'], 'unverified')
        self.assertIn("offset could not be read", out['detail'])

    def test_offset_parsing_accepts_bytes_and_plain_lists(self):
        b = FakeBroker()
        b.offsets[KEY] = {b'k': json.dumps({
            'incremental_snapshot_collections': ['public.a'],
            'incremental_snapshot_correlation_id': 'sig'}).encode()}
        self.assertEqual(signals.offset_snapshot(b, KEY),
                         {'collections': ['public.a'], 'correlation_id': 'sig'})
        b.offsets[KEY] = {'k': 'not json', 'j': '[1,2]'}
        self.assertEqual(signals.offset_snapshot(b, KEY),
                         {'collections': [], 'correlation_id': None})


@unittest.skipUnless(os.environ.get('ECDL_TEST_VALKEY'), 'ECDL_TEST_VALKEY not set')
class WaitForSnapshotValkey(unittest.TestCase):
    """The same watch against a real Valkey, with Debezium played by a thread
    that writes the offset and the stream the way Debezium Server does."""

    def setUp(self):
        import valkey
        host, port = os.environ['ECDL_TEST_VALKEY'].rsplit(':', 1)
        self.v = valkey.Valkey(host=host, port=int(port))
        self.v.delete(KEY, STREAM)
        self.addCleanup(self.v.delete, KEY, STREAM)

    def test_signal_read_and_snapshot_finished(self):
        v = self.v
        v.xadd(STREAM, {'key': '{"id":1}', 'value': json.dumps({'op': 'u'})})
        v.hset(KEY, mapping=offset_with())
        baseline = signals.stream_positions(v, [STREAM])
        self.assertIsNotNone(baseline[STREAM])

        def debezium():
            time.sleep(0.3)
            v.hset(KEY, mapping=offset_with(['public.items'], 'sig-1'))
            for i in range(3):
                v.xadd(STREAM, {'key': '{"id":%d}' % i, 'value': json.dumps({'op': 'r'})})
            time.sleep(0.4)
            v.hset(KEY, mapping=offset_with())
        t = threading.Thread(target=debezium)
        t.start()
        out = signals.wait_for_snapshot(v, 'sig-1', ['public.items'], [STREAM], KEY,
                                        baseline, timeout=10, interval=0.1, settle=0.5)
        t.join()
        self.assertEqual(out['status'], 'finished', out)
        self.assertEqual(len(out['evidence']), 2, out)

    def test_nothing_happens(self):
        v = self.v
        v.hset(KEY, mapping=offset_with())
        baseline = signals.stream_positions(v, [STREAM])
        self.assertEqual(baseline, {STREAM: None})
        out = signals.wait_for_snapshot(v, 'sig-2', ['public.items'], [STREAM], KEY,
                                        baseline, timeout=0.5, interval=0.1)
        self.assertEqual(out['status'], 'unverified', out)

    def test_entries_applied_and_deleted_before_the_watch_saw_them(self):
        # The sink deletes applied entries at once; the advance is still seen.
        v = self.v
        v.xadd(STREAM, {'value': json.dumps({'op': 'u'})})
        baseline = signals.stream_positions(v, [STREAM])
        new_id = v.xadd(STREAM, {'value': json.dumps({'op': 'r'})})
        v.xdel(STREAM, new_id)
        out = signals.wait_for_snapshot(v, 'sig-3', ['public.items'], [STREAM], KEY,
                                        baseline, timeout=0.3, interval=0.1)
        self.assertEqual(out['status'], 'unverified', out)
        self.assertIn('received entries after the signal', out['detail'])


# ── the CLI ────────────────────────────────────────────────────────────────


class SignalCli(unittest.TestCase):

    def setUp(self):
        from ecdl_link.tests.test_cli_apply import CLI
        import ecdl_link.resolve as resolve_mod
        self.CLI = CLI
        self.saved = [(resolve_mod, 'resolve_link', resolve_mod.resolve_link),
                      (CLI, 'load_file', CLI.load_file), (CLI, 'problems_for', CLI.problems_for),
                      (CLI, '_cloudformation', CLI._cloudformation)]
        self.addCleanup(self.restore)
        res = {'name': 'shop', 'source': {'engine': 'postgresql', 'username': 'cdc'},
               'tables': [{'table': 'orders', 'input': 'orders', 'qualified': 'public.orders',
                           'stream': 'data:shop.public.orders'}],
               'debezium': {'signal_collection': SIGNAL}}
        resolve_mod.resolve_link = lambda doc, region=None, profile=None: res
        CLI.load_file = lambda path: {}
        CLI.problems_for = lambda doc: []

    def restore(self):
        for obj, name, value in self.saved:
            setattr(obj, name, value)

    def run_cli(self, action, cfn_params=None, cfn_error=None, **extra):
        class Cfn(object):
            def describe_stacks(self, StackName=None):
                if cfn_error:
                    raise RuntimeError(cfn_error)
                if cfn_params is None:
                    raise RuntimeError('Stack with id x does not exist')
                return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Parameters': [
                    {'ParameterKey': k, 'ParameterValue': v} for k, v in cfn_params.items()]}]}
        self.CLI._cloudformation = lambda profile, region: Cfn()
        extra.setdefault('wait', 0)
        args = argparse.Namespace(action=action, file=__file__, region=None, profile=None,
                                  tables=None, **extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.CLI.cmd_signal(args)
        return out.getvalue()

    def test_ddl_for_a_new_link_uses_its_own_publication(self):
        out = self.run_cli('ddl')
        self.assertIn('CREATE TABLE IF NOT EXISTS {}'.format(SIGNAL), out)
        self.assertIn('CREATE PUBLICATION ecdl_shop_pub FOR TABLE public.orders, {};'
                      .format(SIGNAL), out)
        self.assertIn('GRANT INSERT ON {} TO cdc;'.format(SIGNAL), out)

    def test_ddl_for_a_legacy_link_uses_dbz_pub(self):
        out = self.run_cli('ddl', cfn_params={'PgSlotName': ''})
        self.assertIn('ALTER PUBLICATION dbz_pub ADD TABLE public.orders, {};'
                      .format(SIGNAL), out)

    def test_ddl_says_when_it_could_not_tell(self):
        out = self.run_cli('ddl', cfn_error='ExpiredToken')
        self.assertIn('Could not read the deployed CDC stack (ExpiredToken)', out)
        self.assertIn('ecdl_shop_pub', out)

    def test_snapshot_mentions_the_publication_on_postgres(self):
        out = self.run_cli('snapshot')
        self.assertIn('INSERT INTO {} (id, type, data)'.format(SIGNAL), out)
        self.assertIn("must be in the link's publication", out)

    def test_snapshot_wait_without_a_broker_is_unverified(self):
        orig = self.CLI._snapshot_watch
        self.CLI._snapshot_watch = lambda args, res, cols: 'no route to the broker'
        try:
            out = self.run_cli('snapshot', wait=1)
        finally:
            self.CLI._snapshot_watch = orig
        self.assertIn('unverified: cannot watch', out)

    def test_snapshot_wait_reports_the_result(self):
        b = FakeBroker()
        b.offsets[KEY] = offset_with(['public.orders'], 'x')
        orig_watch = self.CLI._snapshot_watch
        orig_wait = signals.wait_for_snapshot
        seen = {}

        def fake_wait(broker, signal_id, collections, streams, key, baseline, timeout):
            seen.update(signal_id=signal_id, collections=collections, timeout=timeout)
            return {'status': 'consumed', 'detail': 'Debezium read the signal',
                    'evidence': ['offset lists it']}
        self.CLI._snapshot_watch = lambda args, res, cols: (b, ['s'], KEY, {'s': None})
        signals.wait_for_snapshot = fake_wait
        import builtins
        orig_input = builtins.input
        builtins.input = lambda prompt='': ''
        try:
            out = self.run_cli('snapshot', wait=30)
        finally:
            self.CLI._snapshot_watch = orig_watch
            signals.wait_for_snapshot = orig_wait
            builtins.input = orig_input
        self.assertIn('consumed: Debezium read the signal', out)
        self.assertEqual(seen['collections'], ['public.orders'])
        self.assertEqual(seen['timeout'], 30)
        self.assertTrue(seen['signal_id'].startswith('execute-snapshot-'))


if __name__ == '__main__':
    unittest.main()
