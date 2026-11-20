"""The CDC stack's alarms: present in the template and wired from the link."""

import copy
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from ecdl_link.provision import DEFAULT_SINK, plan  # noqa: E402
from ecdl_link.tests.test_provision import resolution  # noqa: E402

CDC_TEMPLATE = os.path.join(_ROOT, 'infra', 'ecdl-cdc-ecs.yaml')


def _cdc_params(resolved):
    step = next(s for s in plan(resolved)['steps'] if s['id'] == 'cdc')
    return step['parameters']


class TemplateTest(unittest.TestCase):
    def setUp(self):
        with open(CDC_TEMPLATE) as handle:
            self.text = handle.read()

    def test_every_failure_mode_has_an_alarm(self):
        for name in ('CaptureNotRunningAlarm', 'CaptureStaleAlarm', 'SinkNotRunningAlarm', 'SinkLagAlarm',
                     'DeadLetterAlarm', 'DebeziumErrorAlarm', 'SinkErrorAlarm'):
            self.assertIn('  {}:\n    Type: AWS::CloudWatch::Alarm'.format(name), self.text)

    def test_alarm_metrics_are_the_ones_the_sink_publishes(self):
        metrics_go = os.path.join(_ROOT, 'tools', 'go-sink', 'metrics.go')
        with open(metrics_go) as handle:
            sink = handle.read()
        # DeadLettered, not DLQDepth: the dead-letter alarm counts new dead
        # letters per minute (Jira story F094); the depth would latch in ALARM.
        for metric in ('OldestEntryAgeSeconds', 'DeadLettered', 'BacklogPerTask', 'TargetUp'):
            self.assertIn('MetricName: {}'.format(metric), self.text)
            self.assertTrue('"{}"'.format(metric) in sink, metric)
        with open(os.path.join(_ROOT, 'tools', 'go-sink', 'capture.go')) as handle:
            capture = handle.read()
        self.assertIn('MetricName: CaptureStaleSeconds', self.text)
        self.assertIn('"CaptureStaleSeconds"', capture)

    def _block(self, name):
        start = self.text.index('  {}:\n'.format(name))
        nxt = re.search(r'\n  [A-Za-z]+:\n', self.text[start + 3:])
        return self.text[start:start + 3 + nxt.start()] if nxt else self.text[start:]

    def test_capture_stale_alarm(self):  # D5
        block = self._block('CaptureStaleAlarm')
        self.assertIn('TreatMissingData: breaching', block)
        self.assertIn('Threshold: !Ref CaptureStaleAlarmSeconds', block)
        self.assertIn("Value: !Sub '${AWS::StackName}-sink'", block)
        self.assertIn('Condition: Alarms', block)

    def test_the_sink_reads_the_heartbeat_debezium_writes_and_its_service(self):
        hb = "!Sub 'data:__debezium-heartbeat.${TopicPrefix}'"
        self.assertIn('{ Name: CAPTURE_HEARTBEAT_STREAM, Value: %s }' % hb, self.text)
        self.assertIn('{ Name: TRIM_STREAMS, Value: %s }' % hb, self.text)
        self.assertIn("{ Name: CAPTURE_SERVICE_NAME, Value: !Sub '${AWS::StackName}-debezium' }", self.text)
        self.assertIn("ServiceName: !Sub '${AWS::StackName}-debezium'", self.text)

    def test_the_task_role_may_read_only_its_debezium_service(self):
        block = self._block('TaskRolePolicy')
        self.assertIn('Action: ecs:DescribeServices', block)
        self.assertIn("service/${ClusterName}/${AWS::StackName}-debezium'", block)
        self.assertNotIn("service/*", block)

    def test_table_stale_alarm_exists_only_when_tables_are_listed(self):  # Jira story F089
        block = self._block('TableStaleAlarm')
        self.assertIn('Condition: TableStaleAlarms', block)
        self.assertIn('MetricName: StaleTables', block)
        self.assertIn('{ Name: TABLE_STALE_SECONDS, Value: !Ref TableStaleSeconds }', self.text)
        with open(os.path.join(_ROOT, 'tools', 'go-sink', 'freshness.go')) as handle:
            self.assertIn('"StaleTables"', handle.read())

    def test_the_parameter_pattern_accepts_what_provision_sends(self):
        from ecdl_link.provision import table_stale_parameter
        block = self._block('TableStaleSeconds')
        pattern = re.search(r"AllowedPattern: '(.+)'", block).group(1)
        for value in (table_stale_parameter({'orders': 3600}),
                      table_stale_parameter({'Inventory.orders': 60, 'b_2': 600}), ''):
            self.assertRegex(value, pattern)
        self.assertNotRegex('orders=3600,', pattern)

    def test_the_boundary_allows_the_read(self):
        with open(os.path.join(_ROOT, 'infra', 'ecdl-control-plane.yaml')) as handle:
            cp = handle.read()
        start = cp.index('  TaskRoleBoundary:\n')
        block = cp[start:cp.index('\n  DeployLinksSupportPolicy:', start)]
        self.assertIn('Action: ecs:DescribeServices', block)
        self.assertIn("service/${ManagedResourcePrefix}-*/${ManagedResourcePrefix}-*'", block)

    def test_both_services_roll_back_a_bad_deployment(self):
        self.assertEqual(len(re.findall(r'DeploymentCircuitBreaker:\n\s+Enable: true\n'
                                        r'\s+Rollback: true', self.text)), 2)

    def test_no_hardcoded_partition_or_cache_port(self):
        self.assertNotIn("'arn:aws:", self.text)
        self.assertNotIn('FromPort: 6379', self.text)


class WiringTest(unittest.TestCase):
    def test_defaults_send_nothing(self):
        params = _cdc_params(resolution('cdc'))
        for key in ('AlarmsEnabled', 'AlarmEmail', 'SinkLagAlarmSeconds', 'CaptureStaleAlarmSeconds',
                    'TableStaleSeconds'):
            self.assertNotIn(key, params)

    def test_link_overrides_are_sent(self):
        r = copy.deepcopy(resolution('cdc'))
        r.setdefault('link', {}).setdefault('advanced', {})['alarms'] = {
            'email': 'ops@example.com', 'sink_lag_seconds': 120,
            'capture_stopped_minutes': 60, 'capture_stale_seconds': 600,
            'table_stale_seconds': {'products': 600, 'Inventory.orders': 3600}}
        params = _cdc_params(r)
        self.assertEqual(params['AlarmEmail'], 'ops@example.com')
        self.assertEqual(params['SinkLagAlarmSeconds'], '120')
        self.assertEqual(params['CaptureStoppedAlarmMinutes'], '60')
        self.assertEqual(params['CaptureStaleAlarmSeconds'], '600')
        self.assertEqual(params['TableStaleSeconds'], 'orders=3600,products=600')

    def test_disabled(self):
        r = copy.deepcopy(resolution('cdc'))
        r.setdefault('link', {}).setdefault('advanced', {})['alarms'] = {'enabled': False}
        self.assertEqual(_cdc_params(r)['AlarmsEnabled'], 'false')

    def test_sink_minimum_matches_the_template_default(self):
        with open(CDC_TEMPLATE) as handle:
            text = handle.read()
        block = text[text.index('  SinkMinCount:'):]
        default = re.search(r'Default: (\d+)', block).group(1)
        self.assertEqual(str(DEFAULT_SINK['min_count']), default)


if __name__ == '__main__':
    unittest.main()
