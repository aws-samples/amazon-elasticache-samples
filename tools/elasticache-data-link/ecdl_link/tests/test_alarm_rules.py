"""How the CDC stack's alarms behave, not only what they are called.

Jira story F093  a dead metrics loop alarms (sink-metrics-missing), instead of leaving the
      metric-driven alarms OK.
Jira story F094  each new dead letter notifies; the old alarm on DLQDepth latched.
Jira story F095  sink-errors fires on 2 of 3 five-minute periods.
Jira story F097  broker-memory watches up to three members and alarms when none reports.
Jira story F180  CaptureStoppedAlarmMinutes cannot exceed what CloudWatch accepts.
Jira story F197  every alarm description points at a section of docs/operations.md.
Jira story F103  capture-not-running ignores a planned pause; capture-paused alarms on
      one that lasts longer than a load can.
Jira story F024  the broker lag and failover alarms watch a third member.

`evaluate` below is CloudWatch's static-threshold evaluation for the settings
these alarms use (M of N periods, TreatMissingData), so the tests can replay a
sequence of datapoints and count the notifications an operator would get.
"""
import json
import os
import re
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link.safe_yaml import load_cfn  # noqa: E402

CDC = os.path.join(ROOT, 'infra', 'ecdl-cdc-ecs.yaml')
OPS = os.path.join(ROOT, 'docs', 'operations.md')
SINK = os.path.join(ROOT, 'tools', 'go-sink')


def _cdc():
    with open(CDC) as handle:
        return load_cfn(handle)


def _ops_anchors():
    """GitHub's anchors for the headings of operations.md."""
    with open(OPS) as handle:
        text = handle.read()
    anchors = set()
    for heading in re.findall(r'^#{1,6} (.+)$', text, re.M):
        slug = re.sub(r'[^\w\- ]', '', heading.strip().lower()).replace(' ', '-')
        anchors.add(slug)
    return anchors


_OPS = {'GreaterThanThreshold': lambda v, t: v > t,
        'GreaterThanOrEqualToThreshold': lambda v, t: v >= t,
        'LessThanThreshold': lambda v, t: v < t}


def evaluate(props, values, threshold=None):
    """Replay `values` (one per period; None = no datapoint) through an alarm.

    Returns the notifications sent, as a list of 'ALARM' / 'OK'. The state
    starts INSUFFICIENT_DATA, as a new alarm does.
    """
    n = props['EvaluationPeriods']
    m = props.get('DatapointsToAlarm', n)
    limit = props['Threshold'] if threshold is None else threshold
    compare = _OPS[props['ComparisonOperator']]
    missing = props.get('TreatMissingData', 'missing')
    actions = {'ALARM': bool(props.get('AlarmActions')), 'OK': bool(props.get('OKActions'))}
    state, sent = 'INSUFFICIENT_DATA', []
    for i in range(len(values)):
        window = values[max(0, i - n + 1):i + 1]
        if all(v is None for v in window) and missing in ('missing', 'ignore'):
            continue                                   # keeps its state
        breaching = 0
        for v in window:
            if v is None:
                breaching += missing == 'breaching'
            else:
                breaching += compare(v, limit)
        new = 'ALARM' if breaching >= m else 'OK'
        if new != state:
            state = new
            if actions[new]:
                sent.append(new)
    return sent


class SinkMetricsMissingTest(unittest.TestCase):  # Jira story F093
    def setUp(self):
        self.res = _cdc()['Resources']
        self.props = self.res['SinkMetricsMissingAlarm']['Properties']

    def test_alarms_on_no_target_up_samples(self):
        p = self.props
        self.assertEqual(self.res['SinkMetricsMissingAlarm']['Condition'], 'Alarms')
        self.assertEqual((p['Namespace'], p['MetricName'], p['Statistic']),
                         ('ECDL/CDCSink', 'TargetUp', 'SampleCount'))
        self.assertEqual(p['TreatMissingData'], 'breaching')
        self.assertEqual(p['Dimensions'], self.res['SinkLagAlarm']['Properties']['Dimensions'])
        self.assertEqual(p['AlarmActions'], [{'Ref': 'AlarmTopic'}])
        self.assertEqual(p['OKActions'], [{'Ref': 'AlarmTopic'}])

    def test_a_publishing_sink_is_ok_and_a_silent_one_alarms_within_5_minutes(self):
        # Three samples a minute per task (METRIC_INTERVAL_SEC=20).
        self.assertEqual(evaluate(self.props, [6] * 10), ['OK'])
        sent = evaluate(self.props, [6] * 5 + [None] * 5)
        self.assertEqual(sent, ['OK', 'ALARM'])
        # One late minute is not an alarm.
        self.assertEqual(evaluate(self.props, [6, 6, None, 6, 6, 6]), ['OK'])

    def test_target_up_is_published_whatever_the_broker_state(self):
        # The alarm relies on TargetUp being in every publish: it is sent before
        # the broker levels, which are left out when the broker cannot be read.
        with open(os.path.join(SINK, 'metrics.go')) as handle:
            go = handle.read()
        body = go[go.index('func levelDatums('):]
        self.assertLess(body.index('d("TargetUp"'), body.index('if !brokerOK'))

    def test_the_config_error_is_an_error_line(self):
        with open(os.path.join(SINK, 'metrics.go')) as handle:
            go = handle.read()
        self.assertIn('logErr("metric-config"', go)
        self.assertNotIn('Backlog metric: disabled (AWS config error', go)


class DeadLetterTest(unittest.TestCase):  # Jira story F094
    def setUp(self):
        self.props = _cdc()['Resources']['DeadLetterAlarm']['Properties']

    def test_counts_new_dead_letters_per_minute(self):
        p = self.props
        self.assertEqual((p['MetricName'], p['Statistic'], p['Period']), ('DeadLettered', 'Sum', 60))
        self.assertEqual((p['Threshold'], p['ComparisonOperator']), (0, 'GreaterThanThreshold'))
        with open(os.path.join(SINK, 'metrics.go')) as handle:
            self.assertIn('{"DeadLettered", c.dlq}', handle.read())

    def test_two_episodes_without_a_replay_notify_twice(self):
        # Per-minute DeadLettered: one at minute 1, another at minute 30, nothing
        # replayed in between.
        minutes = [0] * 40
        minutes[1], minutes[30] = 1, 3
        self.assertEqual(evaluate(self.props, minutes), ['ALARM', 'ALARM'])
        # The old alarm on the depth latched: one notification for both.
        old = dict(self.props, OKActions=[{'Ref': 'AlarmTopic'}])
        depth = [0] + [1] * 29 + [4] * 10
        self.assertEqual(evaluate(old, depth), ['OK', 'ALARM'])

    def test_no_ok_notification_that_reads_as_replayed(self):
        self.assertNotIn('OKActions', self.props)

    def test_the_sink_logs_each_dead_letter(self):
        with open(os.path.join(SINK, 'reliability.go')) as handle:
            go = handle.read()
        to_dlq = go[go.index('func deadLetter('):go.index('func logDeadLetter(')]
        self.assertIn('logDeadLetter(stream, m.ID, reason, retries, why)', to_dlq)
        # logged only once the dead letter is written
        self.assertLess(to_dlq.index('pipe.Exec(ctx)'), to_dlq.index('logDeadLetter('))
        self.assertIn('"error":         why', to_dlq)


class SinkErrorsTest(unittest.TestCase):  # Jira story F095
    def setUp(self):
        res = _cdc()['Resources']
        self.props = res['SinkErrorAlarm']['Properties']
        self.filter = res['SinkErrorMetric']['Properties']

    def test_two_of_three_periods(self):
        self.assertEqual((self.props['EvaluationPeriods'], self.props['DatapointsToAlarm']), (3, 2))
        self.assertEqual(self.filter['FilterPattern'], '?"ERROR" ?"FATAL"')

    def test_errors_every_eleven_minutes_alarm_and_one_isolated_error_does_not(self):
        # Five-minute periods. Errors at minutes 0, 11, 22: periods 1,0,1,0,1.
        self.assertIn('ALARM', evaluate(self.props, [1, 0, 1, 0, 1, 0, 0, 0]))
        three_of_three = dict(self.props, DatapointsToAlarm=3)
        self.assertEqual(evaluate(three_of_three, [1, 0, 1, 0, 1]), ['OK'])
        self.assertEqual(evaluate(self.props, [0, 1, 0, 0, 0]), ['OK'])


class BrokerMemoryTest(unittest.TestCase):  # Jira story F097
    def setUp(self):
        self.props = _cdc()['Resources']['BrokerMemoryAlarm']['Properties']
        self.metrics = {m['Id']: m for m in self.props['Metrics']}

    def _members(self):
        out = []
        for m in self.props['Metrics']:
            stat = m.get('MetricStat')
            if stat:
                self.assertEqual(stat['Metric']['MetricName'], 'DatabaseMemoryUsagePercentage')
                out.append(stat['Metric']['Dimensions'][0]['Value']['Sub'])
        return out

    def test_three_members_and_the_highest(self):
        self.assertEqual(self._members(), ['${BrokerReplicationGroupId}-001',
                                           '${BrokerReplicationGroupId}-002',
                                           '${BrokerReplicationGroupId}-003'])
        self.assertEqual(self.metrics['memory']['Expression'], 'MAX([node1, node2, node3])')
        self.assertEqual([m['Id'] for m in self.props['Metrics'] if m.get('ReturnData')], ['memory'])

    def _series(self, nodes):
        """MAX over the members that report, per minute (None: none reports)."""
        out = []
        for values in zip(*nodes):
            present = [v for v in values if v is not None]
            out.append(max(present) if present else None)
        return out

    def test_a_third_member_over_the_threshold_alarms(self):
        quiet, absent = [40] * 5, [None] * 5
        self.assertEqual(evaluate(self.props, self._series([quiet, quiet, [95] * 5]), 80),
                         ['OK', 'ALARM'])
        self.assertEqual(evaluate(self.props, self._series([quiet, quiet, absent]), 80), ['OK'])

    def test_a_broker_that_stops_reporting_alarms(self):
        self.assertEqual(self.props['TreatMissingData'], 'breaching')
        gone = self._series([[40] * 3 + [None] * 4, [40] * 3 + [None] * 4, [None] * 7])
        self.assertEqual(evaluate(self.props, gone, 80), ['OK', 'ALARM'])


class CaptureNotRunningTest(unittest.TestCase):  # Jira story F103
    """A planned pause no longer holds capture-not-running in ALARM."""
    def setUp(self):
        res = _cdc()['Resources']
        self.props = dict(res['CaptureNotRunningAlarm']['Properties'],
                          EvaluationPeriods=30, DatapointsToAlarm=30)
        self.metrics = {m['Id']: m for m in self.props['Metrics']}

    def _series(self, cpu, paused):
        """cpu + paused per minute; metric math counts a missing value as 0,
        and a minute where both are missing has no datapoint."""
        return [None if c is None and p is None else (c or 0) + (p or 0)
                for c, p in zip(cpu, paused)]

    def test_the_inputs(self):
        cpu = self.metrics['cpu']['MetricStat']
        self.assertEqual((cpu['Stat'], cpu['Metric']['Namespace'], cpu['Metric']['MetricName']),
                         ('SampleCount', 'AWS/ECS', 'CPUUtilization'))
        self.assertIn({'Name': 'ServiceName', 'Value': {'Sub': '${AWS::StackName}-debezium'}},
                      cpu['Metric']['Dimensions'])
        paused = self.metrics['paused']['MetricStat']
        self.assertEqual((paused['Stat'], paused['Metric']['Namespace'], paused['Metric']['MetricName']),
                         ('Maximum', 'ECDL/CDCSink', 'CapturePaused'))
        self.assertEqual(paused['Metric']['Dimensions'],
                         _cdc()['Resources']['SinkLagAlarm']['Properties']['Dimensions'])
        self.assertEqual(self.metrics['running']['Expression'], 'cpu + paused')
        self.assertEqual([m['Id'] for m in self.props['Metrics'] if m.get('ReturnData')], ['running'])
        with open(os.path.join(SINK, 'capture.go')) as handle:
            self.assertIn('d("CapturePaused"', handle.read())

    def test_a_two_hour_load_stays_ok(self):
        running, paused = [1] * 10 + [None] * 120 + [1] * 10, [0] * 10 + [1] * 120 + [0] * 10
        self.assertEqual(evaluate(self.props, self._series(running, paused)), ['OK'])
        # The old alarm, on the CPU samples alone, fired 30 minutes into it.
        self.assertEqual(evaluate(self.props, running), ['OK', 'ALARM', 'OK'])

    def test_no_task_and_no_pause_alarms(self):
        running, paused = [1] * 10 + [None] * 40, [0] * 50
        self.assertEqual(evaluate(self.props, self._series(running, paused)), ['OK', 'ALARM'])

    def test_no_task_and_no_sink_alarms(self):
        # Neither metric: the expression has no data, which is breaching.
        self.assertEqual(self.props['TreatMissingData'], 'breaching')
        running, paused = [1] * 10 + [None] * 40, [0] * 10 + [None] * 40
        self.assertEqual(evaluate(self.props, self._series(running, paused)), ['OK', 'ALARM'])

    def test_a_crash_loop_is_still_running_here(self):
        # A task that runs some of each minute; capture-stale covers it.
        running = [1, None] * 30
        self.assertEqual(evaluate(self.props, self._series(running, [0] * 60)), ['OK'])


class CapturePausedTest(unittest.TestCase):  # Jira story F103
    def setUp(self):
        self.t = _cdc()
        self.props = self.t['Resources']['CapturePausedAlarm']['Properties']

    def test_hourly_minimum_of_capture_paused(self):
        p = self.props
        self.assertEqual(self.t['Resources']['CapturePausedAlarm']['Condition'], 'Alarms')
        self.assertEqual((p['Namespace'], p['MetricName'], p['Statistic'], p['Period']),
                         ('ECDL/CDCSink', 'CapturePaused', 'Minimum', 3600))
        self.assertEqual(p['Dimensions'], self.t['Resources']['SinkLagAlarm']['Properties']['Dimensions'])
        self.assertEqual(p['EvaluationPeriods'], {'Ref': 'CapturePausedAlarmHours'})
        self.assertEqual((p['Threshold'], p['ComparisonOperator']), (1, 'GreaterThanOrEqualToThreshold'))
        self.assertEqual(p['OKActions'], [{'Ref': 'AlarmTopic'}])

    def test_the_parameter_stays_within_a_day(self):
        param = self.t['Parameters']['CapturePausedAlarmHours']
        self.assertEqual((param['MinValue'], param['MaxValue'], param['Default']), (1, 24, 7))

    def _props(self, hours):
        return dict(self.props, EvaluationPeriods=hours)

    def test_a_load_that_resumes_is_quiet_and_one_left_paused_alarms(self):
        p = self._props(7)
        # Hourly minimums: a 6-hour load, then running again.
        self.assertEqual(evaluate(p, [0, 1, 1, 1, 1, 1, 1, 0, 0]), ['OK'])
        # The control plane died mid-load: paused from hour 1 on.
        self.assertEqual(evaluate(p, [0] + [1] * 8), ['OK', 'ALARM'])
        # An hour with a moment of running (Minimum 0) restarts the count.
        self.assertEqual(evaluate(p, [0] + [1] * 5 + [0] + [1] * 5), ['OK'])

    def test_no_data_does_not_alarm(self):
        self.assertEqual(self.props['TreatMissingData'], 'notBreaching')
        self.assertEqual(evaluate(self._props(7), [None] * 10), ['OK'])


class CapturePausedHoursTest(unittest.TestCase):  # Jira story F103
    def _hours(self, mode, timeout=None, tables=None):
        import copy
        from ecdl_link.provision import capture_paused_hours, plan
        from ecdl_link.tests.test_provision import resolution
        r = copy.deepcopy(resolution(mode))
        if timeout is not None:
            r.setdefault('link', {}).setdefault('advanced', {})['loader'] = {'timeout_minutes': timeout}
        if tables is not None:
            r['tables'] = [dict(r['tables'][0], table='t{}'.format(i)) for i in range(tables)]
        step = next((s for s in plan(r)['steps'] if s['id'] == 'cdc'), None)
        sent = step['parameters'].get('CapturePausedAlarmHours') if step else None
        self.assertEqual(sent, None if capture_paused_hours(r) is None else str(capture_paused_hours(r)))
        return capture_paused_hours(r)

    def test_each_table_at_the_default_timeout_plus_an_hour(self):
        self.assertEqual(self._hours('initial_load_and_cdc', tables=1), 7)
        self.assertEqual(self._hours('initial_load_and_cdc', tables=2), 13)

    def test_the_link_timeout(self):
        self.assertEqual(self._hours('initial_load_and_cdc', timeout=90, tables=1), 3)
        self.assertEqual(self._hours('initial_load_and_cdc', timeout=10, tables=1), 2)

    def test_at_most_a_day(self):
        self.assertEqual(self._hours('initial_load_and_cdc', timeout=4320, tables=1), 24)
        self.assertEqual(self._hours('initial_load_and_cdc', tables=5), 24)

    def test_a_link_that_never_loads_keeps_the_default(self):
        self.assertIsNone(self._hours('cdc'))
        self.assertEqual(_cdc()['Parameters']['CapturePausedAlarmHours']['Default'], 7)


class DlqDroppedTest(unittest.TestCase):
    def setUp(self):
        res = _cdc()['Resources']
        self.res = res
        self.props = res['DlqDroppedAlarm']['Properties']

    def test_any_drop_in_a_minute(self):
        p = self.props
        self.assertEqual(self.res['DlqDroppedAlarm']['Condition'], 'Alarms')
        self.assertEqual((p['Namespace'], p['MetricName'], p['Statistic'], p['Period']),
                         ('ECDL/CDCSink', 'DLQDropped', 'Sum', 60))
        self.assertEqual(p['Dimensions'], self.res['SinkLagAlarm']['Properties']['Dimensions'])
        self.assertEqual((p['Threshold'], p['ComparisonOperator']), (0, 'GreaterThanThreshold'))
        self.assertEqual(p['TreatMissingData'], 'notBreaching')
        with open(os.path.join(SINK, 'metrics.go')) as handle:
            self.assertIn('{"DLQDropped", c.dlqDropped}', handle.read())

    def test_each_episode_notifies_and_ok_is_silent(self):
        # OK a minute later does not bring the dropped dead letters back.
        self.assertNotIn('OKActions', self.props)
        minutes = [0] * 20
        minutes[2], minutes[12] = 1, 40
        self.assertEqual(evaluate(self.props, minutes), ['ALARM', 'ALARM'])
        self.assertEqual(evaluate(self.props, [0] * 20), [])


class BrokerThirdNodeTest(unittest.TestCase):  # Jira story F024
    def setUp(self):
        self.res = _cdc()['Resources']

    def _max_series(self, nodes):
        out = []
        for values in zip(*nodes):
            present = [v for v in values if v is not None]
            out.append(max(present) if present else None)
        return out

    def test_a_lagging_third_member_alarms(self):
        props = self.res['BrokerReplicationLagAlarm']['Properties']
        primary, replica, third = [None] * 6, [0.1] * 6, [0.1, 0.1, 30, 30, 30, 30]
        self.assertEqual(evaluate(props, self._max_series([primary, replica, third]), 5),
                         ['OK', 'ALARM'])
        # A two-node broker: the absent third member changes nothing.
        self.assertEqual(evaluate(props, self._max_series([primary, replica, [None] * 6]), 5), ['OK'])

    def test_a_failover_to_the_third_member_alarms(self):
        props = self.res['BrokerFailoverAlarm']['Properties']
        # IsMaster per node: the primary moves from -001 to -003 at minute 3.
        n1, n2, n3 = [1, 1, 1, 0, 0, 0], [0] * 6, [0, 0, 0, 1, 1, 1]
        changed = [None] + [max(abs(b - a) for a, b in ((x[i - 1], x[i]) for x in (n1, n2, n3)))
                            for i in range(1, 6)]
        self.assertEqual(evaluate(props, changed), ['OK', 'ALARM', 'OK'])


class ParameterLimitsTest(unittest.TestCase):  # Jira story F180
    def test_capture_stopped_minutes_stays_within_a_day(self):
        params = _cdc()['Parameters']
        self.assertEqual(params['CaptureStoppedAlarmMinutes']['MaxValue'], 1440)
        with open(os.path.join(ROOT, 'ecdl_link', 'link.schema.json')) as handle:
            schema = json.load(handle)
        text = json.dumps(schema)
        want = re.search(r'"capture_stopped_minutes": \{[^}]*"maximum": (\d+)', text).group(1)
        self.assertEqual(int(want), 1440)
        props = _cdc()['Resources']['CaptureNotRunningAlarm']['Properties']
        self.assertEqual(props['EvaluationPeriods'], {'Ref': 'CaptureStoppedAlarmMinutes'})
        for m in props['Metrics']:
            if 'MetricStat' in m:
                self.assertEqual(m['MetricStat']['Period'], 60, m['Id'])


def _description(props):
    d = props.get('AlarmDescription')
    if isinstance(d, dict):
        d = d['Sub']
    return ' '.join(d.split())


class DescriptionTest(unittest.TestCase):  # Jira story F197
    def setUp(self):
        self.alarms = {k: v['Properties'] for k, v in _cdc()['Resources'].items()
                       if v['Type'] == 'AWS::CloudWatch::Alarm'}

    def test_every_alarm_points_at_an_existing_section(self):
        anchors = _ops_anchors()
        for name, props in self.alarms.items():
            text = _description(props)
            found = re.findall(r'docs/operations\.md#([a-z0-9-]+)', text)
            self.assertEqual(len(found), 1, name)
            self.assertIn(found[0], anchors, name)

    def test_descriptions_are_short(self):
        # What to check first, not the reasoning (that is in YAML comments).
        for name, props in self.alarms.items():
            self.assertLessEqual(len(_description(props)), 260, name)

    def test_capture_not_running_is_not_about_a_paused_load(self):
        # Jira story F103: a planned pause no longer fires it, so the description no
        # longer sends the operator to check for a load first.
        text = _description(self.alarms['CaptureNotRunningAlarm'])
        self.assertIn('not paused', text)
        self.assertNotIn('expected', text)
        self.assertIn('to 1 task', text)

    def test_a_purged_position_says_data_is_lost_and_how_to_reload(self):
        text = _description(self.alarms['DebeziumErrorAlarm'])
        self.assertIn('lost', text)
        self.assertIn('--reset-position', text)

    def test_sink_errors_no_longer_calls_every_error_a_delay(self):
        self.assertNotIn('delay', _description(self.alarms['SinkErrorAlarm']))

    def test_operations_lists_every_alarm(self):
        with open(OPS) as handle:
            text = handle.read()
        table = text[text.index('## Alarms'):text.index('## Where to look')]
        for props in self.alarms.values():
            suffix = props['AlarmName']['Sub'].replace('${AWS::StackName}', '')
            self.assertIn('`…{}`'.format(suffix), table, suffix)


class AlarmTopicWarningTest(unittest.TestCase):  # Jira story F105
    def _warnings(self, alarms=None, mode='cdc'):
        import copy
        from ecdl_link.provision import plan
        from ecdl_link.tests.test_provision import resolution
        r = copy.deepcopy(resolution(mode))
        if alarms is not None:
            r.setdefault('link', {}).setdefault('advanced', {})['alarms'] = alarms
        return [w for w in plan(r)['warnings'] if 'alarm email' in w]

    def test_no_email_warns(self):
        found = self._warnings()
        self.assertEqual(len(found), 1)
        self.assertIn('AlarmTopicArn', found[0])
        self.assertIn('advanced.alarms.email', found[0])

    def test_an_email_or_alarms_off_does_not(self):
        self.assertEqual(self._warnings({'email': 'ops@example.com'}), [])
        self.assertEqual(self._warnings({'enabled': False}), [])

    def test_a_link_without_capture_has_no_alarms_to_warn_about(self):
        self.assertEqual(self._warnings(mode='initial_load'), [])


if __name__ == '__main__':
    unittest.main()
