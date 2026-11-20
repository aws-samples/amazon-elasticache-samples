"""Jira story F057: recover a link whose stored capture position the source has purged.

The documented recovery (`ecdl apply --reload`) kept the stale offset, so
Debezium failed again after the load, and deleting the key by hand after the
load lost every change made during it. `--reset-position` ("reset_position":
true) deletes the offset and schema history WHILE capture is paused, lets
Debezium record the current position, pauses it again, and only then loads.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ecdl_link.deploy import (  # noqa: E402
    Clients, DeployError, Deployer, add_load_step, broker_offset_probe,
    reset_position_refusal,
)
from ecdl_link.provision import plan  # noqa: E402
from ecdl_link.tests.test_deploy import (  # noqa: E402
    BROKER_OUTPUTS, LOADER_OUTPUTS, FakeCloudFormation, FakeECS, stopped,
)
from ecdl_link.tests.test_provision import resolution  # noqa: E402

NAMES = ('ecdl-checkout-broker', 'ecdl-checkout-cdc', 'ecdl-checkout-loader')
DEBEZIUM = 'ecdl-checkout-cdc-debezium'


def _plan(engine='mysql', mode='initial_load_and_cdc', for_update=False):
    r = copy.deepcopy(resolution(mode))
    r['source']['engine'] = engine
    return add_load_step(plan(r, for_update=for_update))


def _deployed_cfn(ops=None):
    cfn = FakeCloudFormation(
        existing={n: 'CREATE_COMPLETE' for n in NAMES},
        outputs={'ecdl-checkout-broker': BROKER_OUTPUTS, 'ecdl-checkout-cdc': {},
                 'ecdl-checkout-loader': LOADER_OUTPUTS})
    if ops is not None:
        real = cfn.update_stack

        def update_stack(**kw):
            ops.append('update ' + kw['StackName'].rpartition('-')[2])
            return real(**kw)
        cfn.update_stack = update_stack
    return cfn


class Broker(object):
    """The broker as the probe sees it: Debezium records an offset some polls
    after it is started, and stops writing once it is paused."""

    def __init__(self, ops, record_after=1, delete_result=2, readable=True,
                 blind_after_delete=False):
        self.ops = ops
        self.blind_after_delete = blind_after_delete
        self.offset = True              # the stale, purged position
        self.debezium = 0
        self.polls_running = 0
        self.record_after = record_after
        self.delete_result = delete_result
        self.readable = readable
        self.clock = 1794882952000

    def probe(self):
        broker = self

        def probe(plan_doc, outputs):
            if not broker.readable:
                return None
            if broker.blind_after_delete and 'delete offset+history' in broker.ops \
                    and broker.debezium:
                return None
            if broker.debezium and not broker.offset:
                broker.polls_running += 1
                if broker.polls_running > broker.record_after:
                    broker.offset = True
                    broker.ops.append('offset recorded')
            return broker.offset

        def reset_position(plan_doc, outputs):
            broker.ops.append('delete offset+history')
            if broker.delete_result is not None:
                broker.offset = False
            return broker.delete_result

        def broker_time_ms(plan_doc, outputs):
            broker.clock += 1000
            broker.ops.append('clock {}'.format(broker.clock))
            return broker.clock

        probe.reset_position = reset_position
        probe.broker_time_ms = broker_time_ms
        return probe


class ResetECS(FakeECS):
    """Records the order of capture changes, task checks and loads."""

    def __init__(self, ops, broker, stopping=0, **kw):
        super(ResetECS, self).__init__(clusters=['ecdl-checkout'], **kw)
        self.ops, self.broker = ops, broker
        self.stopping = stopping        # polls for which a task is still shutting down

    def update_service(self, cluster=None, service=None, desiredCount=None, **kw):
        self.ops.append('desired {}'.format(desiredCount))
        self.broker.debezium = desiredCount
        self.running[service] = [desiredCount]
        return super(ResetECS, self).update_service(
            cluster=cluster, service=service, desiredCount=desiredCount, **kw)

    def list_tasks(self, cluster=None, family=None, desiredStatus=None, serviceName=None, **_):
        if serviceName and desiredStatus == 'STOPPED':
            self.ops.append('tasks stopped?')
            return {'taskArns': ['arn:dbz/old']}
        if serviceName:
            return {'taskArns': []}
        return {'taskArns': []}

    def describe_tasks(self, cluster=None, tasks=None, **kw):
        if tasks == ['arn:dbz/old']:
            if self.stopping:
                self.stopping -= 1
                return {'tasks': [{'lastStatus': 'DEACTIVATING'}]}
            return {'tasks': [{'lastStatus': 'STOPPED'}]}
        return super(ResetECS, self).describe_tasks(cluster=cluster, tasks=tasks, **kw)

    def run_task(self, overrides=None, **kw):
        env = {e['name']: e['value'] for e in overrides['containerOverrides'][0]['environment']}
        self.ops.append('load {}'.format(env['TABLE']))
        return super(ResetECS, self).run_task(overrides=overrides, **kw)


def _deployer(cfn, ecs, probe, events=None, **kw):
    return Deployer(Clients(cloudformation=cfn, ecs=ecs), template_body=lambda n: 'T',
                    on_progress=(events.append if events is not None else None),
                    poll_seconds=1, sleep=lambda _s: None, offset_probe=probe,
                    offset_wait_seconds=3, capture_wait_seconds=5, **kw)


class OrderTest(unittest.TestCase):
    def run_reset(self, **broker_kw):
        ops, events = [], []
        broker = Broker(ops, **broker_kw)
        ecs = ResetECS(ops, broker)
        d = _deployer(_deployed_cfn(ops), ecs, broker.probe(), events, reset_position=True)
        result = d.run(_plan())
        return ops, events, ecs, d, result

    def test_pause_delete_resume_record_pause_then_load(self):
        ops, _events, _ecs, _d, _result = self.run_reset()
        self.assertEqual([o for o in ops if not o.startswith('clock')], [
            'update broker', 'update cdc',
            'update loader',                          # before the pause (Jira story F086)
            'desired 0', 'tasks stopped?',           # the old task has exited
            'delete offset+history',
            'desired 1', 'offset recorded',          # a fresh position exists
            'desired 0', 'tasks stopped?',           # paused again, fully stopped
            'load orders', 'load customers',
            'update loader',                          # marked complete (Jira story F059)
            'desired 1',
            'update cdc'])                            # OffsetCommitted recorded (Jira story F005)

    def test_the_new_position_is_waited_for_not_assumed(self):
        ops, events, _ecs, _d, _r = self.run_reset(record_after=2)
        waits = [e for e in events if e['event'] == 'offset_waiting'
                 and e['step'] == 'reset_resume_capture']
        self.assertEqual(len(waits), 2)
        self.assertLess(ops.index('offset recorded'), ops.index('load orders'))

    def test_a_cold_start_gets_the_capture_start_budget(self):
        # offset_wait_seconds is 3 here and capture_wait_seconds 5: a Debezium
        # that needs four polls to record its first position is still waited for.
        ops, _events, _ecs, _d, _r = self.run_reset(record_after=4)
        self.assertIn('load orders', ops)

    def test_the_strict_wait_is_only_for_the_reset(self):
        ops = []
        broker = Broker(ops)
        d = _deployer(_deployed_cfn(ops), ResetECS(ops, broker), broker.probe(),
                      reset_position=True, allow_unverified_offset=True)
        d.run(_plan())
        self.assertEqual((d.allow_unverified_offset, d.offset_wait_seconds), (True, 3))

    def test_loaded_rows_are_versioned_at_the_second_pause(self):
        _ops, events, ecs, d, _r = self.run_reset()
        versions = [e['version'] for e in events if e['event'] == 'load_version']
        self.assertEqual(len(set(versions)), len(versions))
        env = {e['name']: e['value'] for e in
               ecs.run_tasks[0]['overrides']['containerOverrides'][0]['environment']}
        self.assertEqual(env['LOAD_VERSION'], versions[-1])
        self.assertEqual(d.load_version, versions[-1])
        # Read from the broker clock only once Debezium had fully stopped.
        read_at = _ops.index('clock {}'.format(env['LOAD_VERSION'][:-2]))
        load_at = _ops.index('load orders')
        last_check = max(i for i, o in enumerate(_ops[:load_at]) if o == 'tasks stopped?')
        self.assertGreater(read_at, last_check)

    def test_the_steps_are_reported(self):
        _ops, events, _ecs, _d, result = self.run_reset()
        names = [e['event'] for e in events]
        self.assertIn('position_reset', names)
        steps = [e['step'] for e in events if e['event'] == 'action_complete']
        self.assertEqual(steps, ['pause_capture', 'reset_resume_capture',
                                 'reset_pause_capture', 'resume_capture'])
        self.assertTrue(result['load_recorded'])

    def test_no_stale_offset_needed_before_the_pause(self):
        # The key may already be gone (deleted by hand): no 180s wait, no refusal.
        ops, events = [], []
        broker = Broker(ops)
        broker.offset = False
        probe = broker.probe()
        ecs = ResetECS(ops, broker)
        d = _deployer(_deployed_cfn(ops), ecs, probe, events, reset_position=True)
        # _check_reset reads the broker: False (no key) is readable, so it goes ahead.
        d.run(_plan())
        self.assertIn('load orders', ops)
        self.assertNotIn('pause_capture', [e.get('step') for e in events
                                           if e['event'] == 'offset_waiting'])

    def test_a_task_still_stopping_is_waited_for_before_the_delete(self):
        ops = []
        broker = Broker(ops)
        ecs = ResetECS(ops, broker, stopping=2)
        _deployer(_deployed_cfn(ops), ecs, broker.probe(), reset_position=True).run(_plan())
        first_delete = ops.index('delete offset+history')
        self.assertEqual(ops[:first_delete].count('tasks stopped?'), 3)

    def test_without_the_flag_nothing_is_deleted(self):
        ops = []
        broker = Broker(ops)
        ecs = ResetECS(ops, broker)
        _deployer(_deployed_cfn(ops), ecs, broker.probe()).run(_plan())
        self.assertNotIn('delete offset+history', ops)
        self.assertEqual([o for o in ops if o.startswith('desired')], ['desired 0', 'desired 1'])


class FailureTest(unittest.TestCase):
    def _run(self, broker, stopping=0, **kw):
        ops = broker.ops
        ecs = ResetECS(ops, broker, stopping=stopping)
        cfn = _deployed_cfn(ops)
        d = _deployer(cfn, ecs, broker.probe(), reset_position=True, **kw)
        with self.assertRaises(DeployError) as caught:
            d.run(_plan())
        return caught.exception, ops, ecs, cfn

    def test_unreadable_broker_refuses_before_any_change(self):
        exc, ops, ecs, cfn = self._run(Broker([], readable=False))
        self.assertIn('cannot be read', str(exc))
        self.assertIn('control plane', exc.hint)
        self.assertEqual((ops, cfn.updated, ecs.updated_services), ([], [], []))

    def test_offset_checked_does_not_cover_a_reset(self):
        exc, ops, _ecs, _cfn = self._run(Broker([], readable=False),
                                         allow_unverified_offset=True)
        self.assertIn('--offset-checked does not cover this', exc.hint)
        self.assertEqual(ops, [])

    def test_a_probe_that_cannot_delete_refuses_before_any_change(self):
        ecs = ResetECS([], Broker([]))
        cfn = _deployed_cfn()
        d = _deployer(cfn, ecs, lambda p, o: True, reset_position=True)
        with self.assertRaises(DeployError):
            d.run(_plan())
        self.assertEqual((cfn.updated, ecs.updated_services), ([], []))

    def test_nothing_deployed_is_refused(self):
        cfn = FakeCloudFormation()
        broker = Broker([])
        ecs = ResetECS([], broker)
        d = _deployer(cfn, ecs, broker.probe(), reset_position=True)
        with self.assertRaises(DeployError) as caught:
            d.run(_plan())
        self.assertIn('no deployed capture position', str(caught.exception))
        self.assertEqual((cfn.created, ecs.updated_services), ([], []))

    def test_a_failed_delete_loads_nothing_and_resumes(self):
        exc, ops, ecs, _cfn = self._run(Broker([], delete_result=None))
        self.assertIn('could not delete', str(exc))
        self.assertFalse(ecs.run_tasks)
        self.assertEqual([o for o in ops if o.startswith('desired')], ['desired 0', 'desired 1'])
        self.assertTrue(exc.capture_resumed)

    def test_no_new_position_loads_nothing_and_resumes(self):
        exc, ops, ecs, _cfn = self._run(Broker([], record_after=99))
        self.assertIn('has not committed a log position', str(exc))
        self.assertFalse(ecs.run_tasks)
        # The reset's brief start already set Debezium to 1, so the failure
        # path does not send it again (Jira story F085); capture is running.
        self.assertEqual([o for o in ops if o.startswith('desired')],
                         ['desired 0', 'desired 1'])
        self.assertTrue(exc.capture_resumed)
        self.assertIn('run the load again', exc.hint)

    def test_a_broker_lost_after_the_delete_is_not_waited_out(self):
        # --offset-checked allows a grace period for the ordinary pause, but the
        # new position must be SEEN: a guess here loses the load window.
        exc, ops, ecs, _cfn = self._run(Broker([], blind_after_delete=True),
                                        allow_unverified_offset=True)
        self.assertIn('could not read', str(exc))
        self.assertFalse(ecs.run_tasks)

    def test_a_task_that_never_stops_is_not_deleted_under(self):
        broker = Broker([])
        ops = broker.ops
        ecs = ResetECS(ops, broker, stopping=99)
        d = _deployer(_deployed_cfn(ops), ecs, broker.probe(), reset_position=True)
        d.stop_wait_seconds = 3
        with self.assertRaises(DeployError) as caught:
            d.run(_plan())
        self.assertIn('still DEACTIVATING', str(caught.exception))
        self.assertNotIn('delete offset+history', ops)
        self.assertFalse(ecs.run_tasks)


class RefusalTest(unittest.TestCase):
    def test_no_flag_no_refusal(self):
        self.assertIsNone(reset_position_refusal(_plan(), reload=False))

    def test_a_reset_needs_a_reload(self):
        err = reset_position_refusal(_plan(), reload=False, reset_position=True)
        self.assertIn('needs a reload', str(err))
        self.assertIn('--reload --reset-position', err.hint)

    def test_a_reset_needs_a_plan_that_pauses_for_a_load(self):
        for p in (_plan(mode='cdc'), _plan(mode='initial_load'), _plan(for_update=True)):
            err = reset_position_refusal(p, reload=True, reset_position=True)
            self.assertIn('initial_load_and_cdc', str(err))

    def test_mysql_needs_nothing_else(self):
        self.assertIsNone(reset_position_refusal(_plan(), reload=True, reset_position=True))

    def test_postgres_needs_the_slot_dropped_first(self):
        err = reset_position_refusal(_plan('postgresql'), reload=True, reset_position=True)
        self.assertIn('replication slot', str(err))
        self.assertIn("SELECT pg_drop_replication_slot('ecdl_checkout');", err.hint)
        self.assertIn('--cluster ecdl-checkout --service ecdl-checkout-cdc-debezium '
                      '--desired-count 0', err.hint)
        self.assertIn('--pg-slot-dropped', err.hint)
        self.assertIsNone(reset_position_refusal(_plan('postgresql'), reload=True,
                                                 reset_position=True, pg_slot_dropped=True))

    def test_the_deployer_refuses_postgres_without_the_slot_too(self):
        broker = Broker([])
        ecs = ResetECS([], broker)
        cfn = _deployed_cfn()
        with self.assertRaises(DeployError) as caught:
            _deployer(cfn, ecs, broker.probe(), reset_position=True).run(_plan('postgresql'))
        self.assertIn('pg_drop_replication_slot', caught.exception.hint)
        self.assertEqual((cfn.updated, ecs.updated_services), ([], []))

    def test_postgres_with_the_slot_dropped_runs_the_same_order(self):
        ops = []
        broker = Broker(ops)
        ecs = ResetECS(ops, broker)
        _deployer(_deployed_cfn(ops), ecs, broker.probe(), reset_position=True,
                  pg_slot_dropped=True).run(_plan('postgresql'))
        self.assertLess(ops.index('offset recorded'), ops.index('load orders'))


class ProbeDeleteTest(unittest.TestCase):
    class Secrets(object):
        def get_secret_value(self, SecretId):
            return {'SecretString': 'tok'}

    def test_deletes_both_keys_and_counts_them(self):
        seen = []

        class Conn(object):
            def delete(self, *keys):
                seen.append(keys)
                return 2
        probe = broker_offset_probe(self.Secrets(), connect=lambda *a: Conn())
        self.assertEqual(probe.reset_position(_plan(), {'broker': BROKER_OUTPUTS}), 2)
        self.assertEqual(seen, [('ecdl:checkout:offsets', 'ecdl:checkout:schema_history')])

    def test_unreachable_or_unknown_broker_is_none(self):
        class Broken(object):
            def delete(self, *keys):
                raise OSError('timed out')
        probe = broker_offset_probe(self.Secrets(), connect=lambda *a: Broken())
        self.assertIsNone(probe.reset_position(_plan(), {'broker': BROKER_OUTPUTS}))
        self.assertIsNone(probe.reset_position(_plan(), {}))
        no_keys = _plan()
        next(s for s in no_keys['steps'] if s['id'] == 'cdc')['parameters'].pop('SchemaHistoryKey')
        self.assertIsNone(probe.reset_position(no_keys, {'broker': BROKER_OUTPUTS}))


@unittest.skipUnless(os.environ.get('ECDL_TEST_VALKEY'), 'set ECDL_TEST_VALKEY=host:port')
class RealBrokerTest(unittest.TestCase):
    """The probe against a real Valkey: the offset is a hash, the history a stream."""

    def test_reset_deletes_what_debezium_writes(self):
        import valkey
        host, port = os.environ['ECDL_TEST_VALKEY'].split(':')
        client = valkey.Valkey(host=host, port=int(port))
        offsets, history = 'ecdl:checkout:offsets', 'ecdl:checkout:schema_history'
        client.delete(offsets, history)
        client.hset(offsets, '["redis",{"server":"checkout"}]', '{"file":"mysql-bin.000042"}')
        client.xadd(history, {'1': '{"ddl":"CREATE TABLE orders ..."}'})

        def connect(h, p, password, ssl, timeout):
            return valkey.Valkey(host=host, port=int(port), socket_timeout=timeout)
        probe = broker_offset_probe(None, connect=connect)
        outputs = {'broker': {'PrimaryEndpoint': host, 'Port': port,
                              'TransitEncryption': 'false'}}
        self.assertTrue(probe(_plan(), outputs))
        self.assertEqual(probe.reset_position(_plan(), outputs), 2)
        self.assertFalse(probe(_plan(), outputs))
        self.assertEqual(client.exists(offsets, history), 0)
        self.assertEqual(probe.reset_position(_plan(), outputs), 0)


if __name__ == '__main__':
    unittest.main()
