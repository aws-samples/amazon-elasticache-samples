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
        for name in ('CaptureNotRunningAlarm', 'SinkNotRunningAlarm', 'SinkLagAlarm',
                     'DeadLetterAlarm', 'DebeziumErrorAlarm', 'SinkErrorAlarm'):
            self.assertIn('  {}:\n    Type: AWS::CloudWatch::Alarm'.format(name), self.text)

    def test_alarm_metrics_are_the_ones_the_sink_publishes(self):
        metrics_go = os.path.join(_ROOT, 'tools', 'go-sink', 'metrics.go')
        with open(metrics_go) as handle:
            sink = handle.read()
        for metric in ('OldestEntryAgeSeconds', 'DLQDepth', 'BacklogPerTask'):
            self.assertIn('MetricName: {}'.format(metric), self.text)
            self.assertTrue('"{}"'.format(metric) in sink, metric)

    def test_both_services_roll_back_a_bad_deployment(self):
        self.assertEqual(len(re.findall(r'DeploymentCircuitBreaker:\n\s+Enable: true\n'
                                        r'\s+Rollback: true', self.text)), 2)

    def test_no_hardcoded_partition_or_cache_port(self):
        self.assertNotIn("'arn:aws:", self.text)
        self.assertNotIn('FromPort: 6379', self.text)


class WiringTest(unittest.TestCase):
    def test_defaults_send_nothing(self):
        params = _cdc_params(resolution('cdc'))
        for key in ('AlarmsEnabled', 'AlarmEmail', 'SinkLagAlarmSeconds'):
            self.assertNotIn(key, params)

    def test_link_overrides_are_sent(self):
        r = copy.deepcopy(resolution('cdc'))
        r.setdefault('link', {}).setdefault('advanced', {})['alarms'] = {
            'email': 'ops@example.com', 'sink_lag_seconds': 120,
            'capture_stopped_minutes': 60}
        params = _cdc_params(r)
        self.assertEqual(params['AlarmEmail'], 'ops@example.com')
        self.assertEqual(params['SinkLagAlarmSeconds'], '120')
        self.assertEqual(params['CaptureStoppedAlarmMinutes'], '60')

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
