"""Jira story F140: a teardown keeps the last error lines of the link's logs.

Deleting a link's stacks deletes their log groups. Before each stack is
deleted, the teardown copies the error and warning lines of its log groups
from the last day into its result, so a link that was torn down can still be
diagnosed. Reading the logs never stops a teardown.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link import teardown  # noqa: E402
from ecdl_link.teardown import Destroyer, destroy_plan, log_groups_of  # noqa: E402
from ecdl_link.safe_yaml import load_cfn  # noqa: E402
from ecdl_link.tests.test_teardown import FakeCloudFormation, FakeECS, _Clients  # noqa: E402

NOW = 1_750_000_000.0


class NotFound(Exception):
    pass


NotFound.__name__ = 'ResourceNotFoundException'


class FakeLogs(object):
    """FilterLogEvents over fixed events per group, `per_page` at a time."""
    def __init__(self, events, calls, per_page=100, fail=None):
        self.events = events            # group -> [(timestamp ms, message)]
        self.calls = calls
        self.per_page = per_page
        self.fail = fail or {}

    def filter_log_events(self, logGroupName, startTime, filterPattern, nextToken=None):
        self.calls.append(('filter', logGroupName, startTime, filterPattern))
        if logGroupName in self.fail:
            raise self.fail[logGroupName]
        if logGroupName not in self.events:
            raise NotFound('The specified log group does not exist.')
        found = [(t, m) for t, m in self.events[logGroupName] if t >= startTime]
        start = int(nextToken or 0)
        page = found[start:start + self.per_page]
        out = {'events': [{'timestamp': t, 'message': m, 'logStreamName': 'sink/sink/abc'}
                          for t, m in page]}
        if start + self.per_page < len(found):
            out['nextToken'] = str(start + self.per_page)
        return out


class LoggingCloudFormation(FakeCloudFormation):
    def __init__(self, calls, **kw):
        super(LoggingCloudFormation, self).__init__(**kw)
        self.calls = calls

    def delete_stack(self, StackName=None, **kw):
        self.calls.append(('delete', StackName))
        return super(LoggingCloudFormation, self).delete_stack(StackName=StackName, **kw)


class LogClients(_Clients):
    def __init__(self, cfn, logs):
        super(LogClients, self).__init__(cfn, FakeECS())
        self._logs = logs

    def logs(self):
        if self._logs is None:
            raise Exception('no logs client')
        return self._logs


STACKS = {'ecdl-shop-cdc': 'CREATE_COMPLETE', 'ecdl-shop-loader': 'CREATE_COMPLETE',
          'ecdl-shop-broker': 'CREATE_COMPLETE'}


def _run(events, per_page=100, fail=None, logs=True):
    calls, progress = [], []
    cfn = LoggingCloudFormation(calls, existing=STACKS)
    client = FakeLogs(events, calls, per_page, fail) if logs else None
    d = Destroyer(LogClients(cfn, client), on_progress=progress.append, poll_seconds=0,
                  delete_cluster=False)
    d.snapshot_wait_tries = 0
    d.now = lambda: NOW
    result = d.run(destroy_plan('shop'))
    return result, calls, progress


def _ms(seconds_ago):
    return int((NOW - seconds_ago) * 1000)


class KeptLogLinesTest(unittest.TestCase):
    def test_each_stacks_lines_are_read_before_it_is_deleted(self):
        events = {'/ecs/ecdl-shop-cdc/sink': [(_ms(60), 'ERROR target-down: READONLY')],
                  '/ecs/ecdl-shop-cdc/debezium': [(_ms(30), 'ERROR io.debezium: lost')],
                  '/ecs/ecdl-shop-broker/broker': [(_ms(10), '{"level":"WARN"}')]}
        result, calls, _ = _run(events)
        order = [(c[0], c[1]) for c in calls]
        for stack, groups in (('ecdl-shop-cdc', ['/ecs/ecdl-shop-cdc/debezium', '/ecs/ecdl-shop-cdc/sink']),
                              ('ecdl-shop-loader', ['/ecs/ecdl-shop-loader']),
                              ('ecdl-shop-broker', ['/ecs/ecdl-shop-broker/broker'])):
            deleted = order.index(('delete', stack))
            for group in groups:
                self.assertLess(order.index(('filter', group)), deleted, group)
        kept = result['follow_up']['kept_log_lines']
        self.assertEqual(sorted(kept), ['/ecs/ecdl-shop-broker/broker', '/ecs/ecdl-shop-cdc/debezium',
                                        '/ecs/ecdl-shop-cdc/sink'])
        self.assertIn('ERROR target-down: READONLY', kept['/ecs/ecdl-shop-cdc/sink'][0])
        self.assertIn('sink/sink/abc', kept['/ecs/ecdl-shop-cdc/sink'][0])

    def test_errors_and_warnings_of_the_last_day(self):
        _, calls, _ = _run({'/ecs/ecdl-shop-cdc/sink': []})
        filters = [c for c in calls if c[0] == 'filter']
        self.assertTrue(filters)
        for _, _, start, pattern in filters:
            self.assertEqual(start, _ms(24 * 3600))
            self.assertEqual(pattern, '?ERROR ?FATAL ?WARN ?Exception')

    def test_only_the_newest_lines_and_short_lines(self):
        many = [(_ms(1000 - i), 'ERROR line {} '.format(i) + 'x' * 2000) for i in range(300)]
        result, _, progress = _run({'/ecs/ecdl-shop-cdc/sink': many}, per_page=40)
        kept = result['follow_up']['kept_log_lines']['/ecs/ecdl-shop-cdc/sink']
        self.assertEqual(len(kept), 50)
        self.assertIn('ERROR line 299 ', kept[-1])
        self.assertIn('ERROR line 250 ', kept[0])
        self.assertTrue(all(len(line) < 600 for line in kept))
        event = [e for e in progress if e['event'] == 'log_lines_kept'
                 and e['group'] == '/ecs/ecdl-shop-cdc/sink'][0]
        self.assertEqual((event['lines'], event['hours']), (50, 24))
        self.assertNotIn('only_part_read', event)

    def test_a_day_too_long_to_read_keeps_the_last_hour(self):
        day = [(_ms(20 * 3600 - i), 'ERROR old {}'.format(i)) for i in range(2000)]
        hour = [(_ms(600 - i), 'ERROR recent {}'.format(i)) for i in range(60)]
        result, _, progress = _run({'/ecs/ecdl-shop-cdc/sink': day + hour}, per_page=50)
        kept = result['follow_up']['kept_log_lines']['/ecs/ecdl-shop-cdc/sink']
        self.assertIn('ERROR recent 59', kept[-1])
        self.assertTrue(all('recent' in line for line in kept))
        event = [e for e in progress if e['event'] == 'log_lines_kept'
                 and e['group'] == '/ecs/ecdl-shop-cdc/sink'][0]
        self.assertEqual(event['hours'], 1)

    def test_a_missing_group_is_skipped_and_an_error_never_stops_the_teardown(self):
        events = {'/ecs/ecdl-shop-cdc/sink': [(_ms(5), 'ERROR x')]}
        fail = {'/ecs/ecdl-shop-cdc/debezium': Exception('AccessDeniedException: not allowed')}
        result, _, progress = _run(events, fail=fail)
        self.assertEqual(result['deleted'], ['ecdl-shop-cdc', 'ecdl-shop-loader', 'ecdl-shop-broker'])
        self.assertEqual(list(result['follow_up']['kept_log_lines']), ['/ecs/ecdl-shop-cdc/sink'])
        failed = [e for e in progress if e['event'] == 'log_lines_not_kept']
        self.assertEqual([e['group'] for e in failed], ['/ecs/ecdl-shop-cdc/debezium'])
        self.assertIn('AccessDenied', failed[0]['error'])

    def test_no_logs_client_still_tears_down(self):
        result, _, progress = _run({}, logs=False)
        self.assertEqual(len(result['deleted']), 3)
        self.assertNotIn('kept_log_lines', result['follow_up'])
        self.assertTrue(any(e['event'] == 'log_lines_not_kept' for e in progress))


class GroupNamesTest(unittest.TestCase):
    def test_the_names_are_the_ones_the_templates_create(self):
        for step, template in (('cdc', 'ecdl-cdc-ecs.yaml'), ('loader', 'ecdl-loader-ecs.yaml'),
                               ('broker', 'ecdl-broker-quickstart.yaml')):
            with open(os.path.join(ROOT, 'infra', template)) as handle:
                res = load_cfn(handle)['Resources']
            names = sorted(r['Properties']['LogGroupName']['Sub'].replace('${AWS::StackName}', 'S')
                           for r in res.values() if r['Type'] == 'AWS::Logs::LogGroup')
            self.assertEqual(sorted(log_groups_of(step, 'S')), names, template)

    def test_the_control_plane_may_read_them(self):
        from ecdl_link.tests.policy_eval import allows
        from ecdl_link.tests.test_control_plane_least_privilege import _arn, _statements
        st = _statements('InstanceRole', {'HasMonitorSourceSecrets': False})
        for step in ('cdc', 'loader', 'broker'):
            for group in log_groups_of(step, 'ecdl-shop-' + step):
                self.assertTrue(allows(st, 'logs:FilterLogEvents', _arn('logs', 'log-group:' + group)), group)

    def test_limits(self):
        self.assertEqual(teardown._KEPT_LOG_LINES, 50)
        self.assertEqual(teardown._KEPT_LOG_HOURS, 24)


if __name__ == '__main__':
    unittest.main()
