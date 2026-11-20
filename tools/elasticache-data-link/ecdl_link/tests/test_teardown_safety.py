"""Jira story F136: a teardown says what the broker holds, refuses to delete under a
running change, and stops a running load before deleting its stack.

broker_backlog is checked against a real Valkey when ECDL_TEST_VALKEY names
one (host:port); those tests skip otherwise. The rest use the fakes from
test_teardown.
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import DeployError  # noqa: E402
from ecdl_link.teardown import (  # noqa: E402
    DLQ_PREFIX, SINK_GROUP, Destroyer, broker_backlog, describe_broker_data, destroy_plan)
from ecdl_link.tests.test_teardown import FakeCloudFormation, FakeECS  # noqa: E402

LIVE = {'ecdl-shop-cdc': 'CREATE_COMPLETE', 'ecdl-shop-loader': 'CREATE_COMPLETE',
        'ecdl-shop-broker': 'CREATE_COMPLETE'}
STREAMS = 'data:shop.Inventory.orders,data:shop.Inventory.items'


def _valkey():
    where = os.environ.get('ECDL_TEST_VALKEY')
    if not where:
        return None
    import valkey
    host, _, port = where.rpartition(':')
    client = valkey.Valkey(host=host or '127.0.0.1', port=int(port), socket_timeout=5)
    client.ping()
    return client


@unittest.skipUnless(os.environ.get('ECDL_TEST_VALKEY'), 'needs ECDL_TEST_VALKEY')
class BrokerBacklogValkeyTest(unittest.TestCase):
    """Counted the way the sink consumes: group lag plus pending entries."""

    def setUp(self):
        self.v = _valkey()
        self.ns = 'mdeploy-{}'.format(uuid.uuid4().hex[:8])
        self.keys = []

    def tearDown(self):
        if self.keys:
            self.v.delete(*self.keys)

    def stream(self, name, entries, group=SINK_GROUP, read=0, ack=0):
        key = 'data:{}.{}'.format(self.ns, name)
        self.keys.append(key)
        ids = [self.v.xadd(key, {'n': str(i)}) for i in range(entries)]
        if group:
            self.v.xgroup_create(key, group, id='0')
            if read:
                got = self.v.xreadgroup(group, 'c1', {key: '>'}, count=read)
                if ack:
                    self.v.xack(key, group, *[i for i, _ in got[0][1][:ack]])
        return key, ids

    def dlq(self, key, entries):
        name = DLQ_PREFIX + key
        self.keys.append(name)
        for i in range(entries):
            self.v.xadd(name, {'err': str(i)})
        return name

    def test_lag_plus_pending_is_the_backlog(self):
        caught_up, _ = self.stream('a', 5, read=5, ack=5)
        behind, _ = self.stream('b', 10, read=6, ack=2)     # 4 pending + 4 undelivered
        found = broker_backlog(self.v, [caught_up, behind])
        self.assertEqual(found['backlog'], {behind: 8})

    def test_a_stream_the_sink_never_read_counts_whole(self):
        key, _ = self.stream('c', 7, group=None)
        self.assertEqual(broker_backlog(self.v, [key])['backlog'], {key: 7})

    def test_a_missing_stream_is_nothing(self):
        self.assertEqual(broker_backlog(self.v, ['data:{}.none'.format(self.ns)])['backlog'], {})

    def test_unknown_lag_falls_back_to_the_length(self):
        key, ids = self.stream('d', 6, read=3, ack=3)
        # Deleting an entry the group has not read makes the lag unknown (nil).
        self.v.xdel(key, ids[4])
        info = {k if isinstance(k, str) else k.decode(): v
                for k, v in self.v.xinfo_groups(key)[0].items()}
        if info.get('lag') is not None:
            self.skipTest('this Valkey still reports a lag after XDEL')
        self.assertEqual(broker_backlog(self.v, [key])['backlog'], {key: 5})

    def test_dead_letters_are_counted_on_every_dlq_stream(self):
        key, _ = self.stream('e', 1, read=1, ack=1)
        dlq = self.dlq(key, 3)
        found = broker_backlog(self.v, [key])
        self.assertEqual(found['backlog'], {})
        self.assertEqual(found['dead_letters'].get(dlq), 3)

    def test_streams_are_found_by_scan_when_the_cdc_stack_is_gone(self):
        key, _ = self.stream('f', 2, read=0)
        heartbeat = 'data:__debezium-heartbeat.{}'.format(self.ns)
        self.keys.append(heartbeat)
        self.v.xadd(heartbeat, {'ts': '1'})
        found = broker_backlog(self.v, None)
        self.assertEqual(found['backlog'].get(key), 2)
        self.assertNotIn(heartbeat, found['backlog'])


class TaskECS(FakeECS):
    """FakeECS with loader tasks that stop after `stops_after` describes."""

    def __init__(self, tasks=(), stops_after=0, stop_error=None, list_error=None, **kw):
        super().__init__(**kw)
        self.tasks = list(tasks)
        self.stops_after = stops_after
        self.stop_error = stop_error
        self.list_error = list_error
        self.stopped = []
        self.listed = []

    def list_tasks(self, cluster=None, family=None, desiredStatus=None, **_):
        self.listed.append((cluster, family, desiredStatus))
        if self.list_error:
            raise Exception(self.list_error)
        return {'taskArns': list(self.tasks)}

    def stop_task(self, cluster=None, task=None, reason=None):
        if self.stop_error:
            raise Exception(self.stop_error)
        self.stopped.append(task)
        return {}

    def describe_tasks(self, cluster=None, tasks=None):
        status = 'STOPPED' if self.stops_after <= 0 else 'RUNNING'
        self.stops_after -= 1
        return {'tasks': [{'taskArn': t, 'lastStatus': status} for t in tasks]}


class Clients(object):
    def __init__(self, cfn, ecs):
        self.cloudformation = cfn
        self.ecs = ecs


def destroyer(cfn, ecs, reader=None, **kw):
    events = []
    d = Destroyer(Clients(cfn, ecs), on_progress=events.append, poll_seconds=0,
                  sleep=lambda _s: None, broker_reader=reader, delete_cluster=False, **kw)
    d.snapshot_wait_tries = 0
    return d, events


def reader(backlog=None, dead=None, seen=None):
    def read(outputs, streams):
        if seen is not None:
            seen.append((outputs, streams))
        return {'backlog': backlog or {}, 'dead_letters': dead or {}}
    return read


class BrokerDataTest(unittest.TestCase):
    def cfn(self, existing=None):
        cfn = FakeCloudFormation(existing=existing or dict(LIVE),
                                 params={'ecdl-shop-cdc': {'Streams': STREAMS}})
        return cfn

    def test_streams_come_from_the_cdc_stack(self):
        seen = []
        d, _ = destroyer(self.cfn(), TaskECS(), reader(seen=seen))
        data = d.broker_data(destroy_plan('shop'))
        self.assertEqual(seen[0][1], STREAMS.split(','))
        self.assertEqual((data['readable'], data['at_risk']), (True, False))

    def test_without_the_cdc_stack_the_broker_is_scanned(self):
        seen = []
        existing = {'ecdl-shop-broker': 'CREATE_COMPLETE'}
        d, _ = destroyer(self.cfn(existing), TaskECS(), reader(seen=seen))
        d.broker_data(destroy_plan('shop'))
        self.assertIsNone(seen[0][1])

    def test_counts_and_summary(self):
        d, _ = destroyer(self.cfn(), TaskECS(), reader({'data:a': 4, 'data:b': 1},
                                                       {'dlq:data:a': 2}))
        data = d.broker_data(destroy_plan('shop'))
        self.assertEqual((data['unapplied'], data['dead_letters_total'], data['at_risk']),
                         (5, 2, True))
        self.assertIn('5 change(s) not yet written', describe_broker_data(data))
        self.assertIn('2 dead letter(s)', describe_broker_data(data))

    def test_no_broker_stack_is_nothing_at_risk(self):
        d, _ = destroyer(self.cfn({'ecdl-shop-cdc': 'CREATE_COMPLETE'}), TaskECS(), reader())
        self.assertEqual(d.broker_data(destroy_plan('shop')), {'exists': False,
                                                               'at_risk': False})

    def test_an_unreachable_broker_is_unknown_not_empty(self):
        def unreachable(outputs, streams):
            raise Exception('Timeout connecting to server')
        d, _ = destroyer(self.cfn(), TaskECS(), unreachable)
        data = d.broker_data(destroy_plan('shop'))
        self.assertEqual((data['readable'], data['at_risk']), (False, False))
        self.assertIn('Timeout connecting', describe_broker_data(data))
        self.assertIn('unknown', describe_broker_data(data))

    def test_the_default_reader_needs_an_endpoint(self):
        # FakeCloudFormation's broker has no outputs: no connection is tried.
        d, _ = destroyer(self.cfn(), TaskECS())
        data = d.broker_data(destroy_plan('shop'))
        self.assertEqual(data['readable'], False)
        self.assertIn('no endpoint output', data['reason'])


class DestroyRefusalTest(unittest.TestCase):
    def run_destroy(self, existing=None, ecs=None, read=None, **kw):
        cfn = FakeCloudFormation(existing=existing or dict(LIVE),
                                 params={'ecdl-shop-cdc': {'Streams': STREAMS,
                                                           'ClusterName': 'ecdl-shop'}})
        d, events = destroyer(cfn, ecs or TaskECS(), read or reader(), **kw)
        try:
            return d.run(destroy_plan('shop')), cfn, events, None
        except DeployError as exc:
            return None, cfn, events, exc

    def test_a_backlog_refuses_before_anything_is_deleted(self):
        ecs = TaskECS(tasks=['arn:task/1'])
        _r, cfn, _e, exc = self.run_destroy(ecs=ecs, read=reader({'data:a': 3}, {'dlq:x': 1}))
        self.assertIn('3 change(s) not yet written to the cache and 1 dead letter', str(exc))
        self.assertIn('--delete-backlog', exc.hint)
        self.assertEqual((cfn.deleted, ecs.stopped), ([], []))

    def test_dead_letters_alone_refuse(self):
        _r, cfn, _e, exc = self.run_destroy(read=reader({}, {'dlq:x': 1}))
        self.assertIsNotNone(exc)
        self.assertEqual(cfn.deleted, [])

    def test_delete_backlog_deletes_and_reports(self):
        result, cfn, events, exc = self.run_destroy(read=reader({'data:a': 3}),
                                                    delete_backlog=True)
        self.assertIsNone(exc)
        self.assertEqual(cfn.deleted, ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])
        data = next(e for e in events if e['event'] == 'broker_data')
        self.assertEqual(data['unapplied'], 3)

    def test_an_unreadable_broker_does_not_block(self):
        def unreachable(outputs, streams):
            raise Exception('no route')
        result, cfn, _e, exc = self.run_destroy(read=unreachable)
        self.assertIsNone(exc)
        self.assertEqual(len(cfn.deleted), 3)

    def test_a_stack_mid_update_refuses(self):
        for state in ('UPDATE_IN_PROGRESS', 'CREATE_IN_PROGRESS',
                      'UPDATE_ROLLBACK_IN_PROGRESS'):
            _r, cfn, _e, exc = self.run_destroy(dict(LIVE, **{'ecdl-shop-cdc': state}))
            self.assertIn('being changed right now', str(exc), state)
            self.assertIn(state, str(exc))
            self.assertEqual(cfn.deleted, [], state)

    def test_a_delete_in_progress_is_finished_not_refused(self):
        _r, cfn, _e, exc = self.run_destroy(dict(LIVE, **{'ecdl-shop-cdc': 'DELETE_IN_PROGRESS'}))
        self.assertIsNone(exc)

    def test_a_running_load_is_stopped_before_its_stack_goes(self):
        ecs = TaskECS(tasks=['arn:task/1', 'arn:task/2'], stops_after=2)
        _r, cfn, events, exc = self.run_destroy(ecs=ecs)
        self.assertIsNone(exc)
        self.assertEqual(ecs.stopped, ['arn:task/1', 'arn:task/2'])
        self.assertEqual(ecs.listed[0], ('ecdl-shop', 'ecdl-shop-loader', 'RUNNING'))
        names = [e['event'] for e in events]
        self.assertLess(names.index('loads_stopped'), names.index('stack_delete_started'))

    def test_a_load_that_will_not_stop_refuses(self):
        ecs = TaskECS(tasks=['arn:task/1'], stops_after=10 ** 6)
        cfn = FakeCloudFormation(existing=dict(LIVE))
        d, _ = destroyer(cfn, ecs, reader())
        d.stop_wait_seconds = 5
        with self.assertRaises(DeployError) as caught:
            d.run(destroy_plan('shop'))
        self.assertIn('still RUNNING', str(caught.exception))
        self.assertEqual(cfn.deleted, [])

    def test_a_stop_that_fails_refuses(self):
        _r, cfn, _e, exc = self.run_destroy(ecs=TaskECS(tasks=['arn:task/1'],
                                                        stop_error='AccessDenied'))
        self.assertIn('could not stop the load task', str(exc))
        self.assertEqual(cfn.deleted, [])

    def test_tasks_that_cannot_be_listed_refuse_but_no_cluster_is_fine(self):
        _r, cfn, _e, exc = self.run_destroy(ecs=TaskECS(list_error='AccessDenied'))
        self.assertIn('could not check for a running load', str(exc))
        self.assertEqual(cfn.deleted, [])
        _r, cfn, _e, exc = self.run_destroy(ecs=TaskECS(list_error='ClusterNotFoundException'))
        self.assertIsNone(exc)


if __name__ == '__main__':
    unittest.main()
