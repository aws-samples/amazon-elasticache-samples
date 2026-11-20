"""Jira story F090: broker durability is observed.

The broker's replica, its failovers, the calls that reboot or remove a node, and
Debezium's own view (WAIT shortfalls, refused writes) each reach the link's
alarm topic. The metric filters are checked against the log lines the Java
broker writer actually prints, so a reworded message cannot silently stop an
alarm.
"""
import fnmatch
import glob
import os
import re
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ecdl_link.tests.test_broker_snapshots import deploy_role_allows  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import _CfnLoader  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CDC = os.path.join(ROOT, 'infra', 'ecdl-cdc-ecs.yaml')
WRITER = os.path.join(ROOT, 'data-plane', 'debezium-server', 'broker-writer', 'src', 'main')

STACK = 'ecdl-shop-cdc'
GROUP = 'ecdl-shop-broker'
ACCOUNT, REGION = '111122223333', 'us-east-2'


def _as_list(value):
    return [value] if isinstance(value, (str, dict)) else list(value or [])


def _cdc():
    with open(CDC) as handle:
        return yaml.load(handle, Loader=_CfnLoader)  # nosec B506 - SafeLoader subclass


def _sub(value, **extra):
    """The text of a !Sub (or a plain string) with this test's names."""
    text = value['Sub'] if isinstance(value, dict) else value
    names = {'AWS::StackName': STACK, 'AWS::AccountId': ACCOUNT, 'AWS::Region': REGION,
             'AWS::Partition': 'aws', 'BrokerReplicationGroupId': GROUP}
    names.update(extra)
    return re.sub(r'\$\{([^}]+)\}', lambda m: names.get(m.group(1), m.group(0)), text)


# ── Java log lines ───────────────────────────────────────────────────────────
_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_CALL = re.compile(r'(?:LOGGER\.(?:trace|debug|info|warn|error)|new DebeziumException)\((.*?)\);[ \t]*\n',
                   re.S)


def writer_messages():
    """Every message the broker writer logs or throws, with {} filled in."""
    out = []
    for path in glob.glob(os.path.join(WRITER, '**', '*.java'), recursive=True):
        with open(path) as handle:
            java = handle.read()
        for call in _CALL.finditer(java):
            text = ''.join(m.group(1) for m in _LITERAL.finditer(call.group(1)))
            if text:
                out.append(text.replace('\\"', '"').replace('{}', '7'))
    return out


def filter_matches(pattern, line):
    """CloudWatch Logs' unstructured filter syntax, the subset used here:
    "a" "b" (all terms) or ?"a" ?"b" (any term). Case sensitive."""
    terms = re.findall(r'(\??)"([^"]*)"', pattern)
    if not terms or re.sub(r'\??"[^"]*"', '', pattern).strip():
        raise ValueError('unsupported filter pattern: ' + pattern)
    optional = {bool(q) for q, _ in terms}
    if len(optional) != 1:
        raise ValueError('mixed ? and plain terms: ' + pattern)
    hits = [t in line for _, t in terms]
    return any(hits) if optional == {True} else all(hits)


class WriterLogFilterTest(unittest.TestCase):
    def setUp(self):
        self.res = _cdc()['Resources']
        self.messages = writer_messages()
        self.assertGreater(len(self.messages), 10)

    def _matching(self, logical):
        pattern = self.res[logical]['Properties']['FilterPattern']
        return [m for m in self.messages if filter_matches(pattern, m)]

    def test_matches_wait_shortfalls_and_refused_writes(self):
        hits = self._matching('BrokerWritesHeldMetric')
        for line in ('no replica acknowledged 7 change(s) within 7 ms (attempt 7/7)',
                     'no replica acknowledged the writes after',
                     'broker memory is full; 7 change(s) waiting',
                     '7 of 7 change(s) not written yet (7); attempt 7'):
            self.assertTrue(any(line in m for m in hits), line)
        self.assertEqual(len(hits), 4, hits)

    def test_does_not_match_recoveries_or_other_lines(self):
        pattern = self.res['BrokerWritesHeldMetric']['Properties']['FilterPattern']
        others = [m for m in self.messages if not filter_matches(pattern, m)]
        for line in ('memory available again', 'written after 7 attempt(s)',
                     'connected to primary', 'so the broker failed over',
                     'reconnected to the same primary'):
            self.assertTrue(any(line in m for m in others), line)

    def test_the_matcher_itself(self):
        self.assertTrue(filter_matches('?"a b" ?"c"', 'xx c'))
        self.assertFalse(filter_matches('"a" "c"', 'a only'))
        self.assertTrue(filter_matches('"a" "c"', 'c and a'))
        self.assertFalse(filter_matches('"A"', 'a'))
        with self.assertRaises(ValueError):
            filter_matches('?"a" "b"', 'a b')
        with self.assertRaises(ValueError):
            filter_matches('[level=ERROR]', 'x')

    def test_each_filter_feeds_its_alarm(self):
        for metric, alarm in (('BrokerWritesHeldMetric', 'BrokerWritesHeldAlarm'),):
            m = self.res[metric]['Properties']
            a = self.res[alarm]['Properties']
            self.assertEqual(m['LogGroupName'], {'Ref': 'DebeziumLogGroup'})
            self.assertEqual(m['MetricTransformations'][0]['MetricName'], a['MetricName'])
            self.assertEqual(m['MetricTransformations'][0]['MetricNamespace'], a['Namespace'])
            self.assertEqual(self.res[metric]['Condition'], 'Alarms')
            self.assertEqual(self.res[alarm]['Condition'], 'Alarms')


class BrokerAlarmTest(unittest.TestCase):
    def setUp(self):
        self.t = _cdc()
        self.res = self.t['Resources']

    def _metrics(self, logical):
        return {m['Id']: m for m in self.res[logical]['Properties']['Metrics']}

    def _nodes(self, metrics, name):
        out = []
        for m in metrics.values():
            stat = m.get('MetricStat')
            if stat:
                self.assertEqual(stat['Metric']['Namespace'], 'AWS/ElastiCache')
                self.assertEqual(stat['Metric']['MetricName'], name)
                dims = {d['Name']: _sub(d['Value']) for d in stat['Metric']['Dimensions']}
                out.append(dims['CacheClusterId'])
        return sorted(out)

    def test_both_conditions_require_alarms_and_a_broker_id(self):
        cond = self.t['Conditions']['BrokerAlarms']
        self.assertIn({'Condition': 'Alarms'}, cond['And'])
        for logical in ('BrokerReplicationLagAlarm', 'BrokerFailoverAlarm', 'BrokerEventsRule'):
            self.assertEqual(self.res[logical]['Condition'], 'BrokerAlarms', logical)

    def test_replication_lag_on_any_of_three_nodes(self):
        # NodeCount 3 makes a <group>-003; the alarm used to watch two (Jira story F024).
        metrics = self._metrics('BrokerReplicationLagAlarm')
        self.assertEqual(self._nodes(metrics, 'ReplicationLag'),
                         [GROUP + '-001', GROUP + '-002', GROUP + '-003'])
        self.assertEqual(metrics['lag']['Expression'], 'MAX([node1, node2, node3])')
        props = self.res['BrokerReplicationLagAlarm']['Properties']
        self.assertEqual(props['Threshold'], {'Ref': 'BrokerReplicationLagAlarmSeconds'})
        self.assertEqual(props['ComparisonOperator'], 'GreaterThanThreshold')
        self.assertEqual(self.t['Parameters']['BrokerReplicationLagAlarmSeconds']['Type'], 'Number')

    def test_failover_is_a_change_of_primary_on_any_of_three_nodes(self):
        metrics = self._metrics('BrokerFailoverAlarm')
        self.assertEqual(self._nodes(metrics, 'IsMaster'),
                         [GROUP + '-001', GROUP + '-002', GROUP + '-003'])
        expr = metrics['changed']['Expression']
        for node in ('node1', 'node2', 'node3'):
            self.assertIn('ABS(DIFF({}))'.format(node), expr)
        props = self.res['BrokerFailoverAlarm']['Properties']
        # DIFF needs a previous point inside the evaluated range.
        self.assertGreaterEqual(props['EvaluationPeriods'], 2)
        self.assertEqual(props['DatapointsToAlarm'], 1)
        self.assertEqual(props['Threshold'], 1)

    def test_every_new_alarm_notifies_the_topic_both_ways(self):
        for logical in ('BrokerReplicationLagAlarm', 'BrokerFailoverAlarm',
                        'BrokerWritesHeldAlarm'):
            props = self.res[logical]['Properties']
            self.assertEqual(props['AlarmActions'], [{'Ref': 'AlarmTopic'}], logical)
            self.assertEqual(props['OKActions'], [{'Ref': 'AlarmTopic'}], logical)
            self.assertTrue(_sub(props['AlarmName']).startswith(STACK + '-'), logical)


# ── EventBridge rule ─────────────────────────────────────────────────────────
def _resolve(node):
    if isinstance(node, dict):
        if set(node) == {'Ref'}:
            return {'BrokerReplicationGroupId': GROUP}.get(node['Ref'], node['Ref'])
        if set(node) == {'Sub'}:
            return _sub(node)
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    return node


def event_matches(pattern, event):
    """EventBridge matching for the constructs the rule uses: value lists,
    {"prefix": ...} and "$or"."""
    for key, want in pattern.items():
        if key == '$or':
            if not any(event_matches(alt, event) for alt in want):
                return False
            continue
        if key not in event:
            return False
        got = event[key]
        if isinstance(want, dict):
            if not isinstance(got, dict) or not event_matches(want, got):
                return False
            continue
        ok = False
        for option in want:
            if isinstance(option, dict) and 'prefix' in option:
                ok = ok or (isinstance(got, str) and got.startswith(option['prefix']))
            else:
                ok = ok or got == option
        if not ok:
            return False
    return True


def _cloudtrail(name, **params):
    return {'source': 'aws.elasticache', 'detail-type': 'AWS API Call via CloudTrail',
            'detail': {'eventSource': 'elasticache.amazonaws.com', 'eventName': name,
                       'requestParameters': params,
                       'userIdentity': {'arn': 'arn:aws:iam::1:user/x'}}}


class BrokerEventsRuleTest(unittest.TestCase):
    def setUp(self):
        self.res = _cdc()['Resources']
        self.rule = self.res['BrokerEventsRule']['Properties']
        self.pattern = _resolve(self.rule['EventPattern'])

    def test_matches_reboots_failovers_and_removals_of_this_broker(self):
        for event in (_cloudtrail('RebootCacheCluster', cacheClusterId=GROUP + '-001',
                                  cacheNodeIdsToReboot=['0001']),
                      _cloudtrail('TestFailover', replicationGroupId=GROUP, nodeGroupId='0001'),
                      _cloudtrail('DeleteCacheCluster', cacheClusterId=GROUP + '-002'),
                      _cloudtrail('DecreaseReplicaCount', replicationGroupId=GROUP),
                      _cloudtrail('DeleteReplicationGroup', replicationGroupId=GROUP)):
            self.assertTrue(event_matches(self.pattern, event), event['detail']['eventName'])

    def test_ignores_other_caches_and_routine_calls(self):
        for event in (_cloudtrail('RebootCacheCluster', cacheClusterId='payments-001'),
                      _cloudtrail('TestFailover', replicationGroupId='ecdl-shop-broker2'),
                      _cloudtrail('RebootCacheCluster', cacheClusterId='ecdl-shop-broker2-001'),
                      _cloudtrail('ModifyReplicationGroup', replicationGroupId=GROUP),
                      _cloudtrail('DescribeReplicationGroups', replicationGroupId=GROUP),
                      dict(_cloudtrail('TestFailover', replicationGroupId=GROUP),
                           source='aws.rds')):
            self.assertFalse(event_matches(self.pattern, event), event)

    def test_publishes_a_readable_message_to_the_alarm_topic(self):
        target = self.rule['Targets'][0]
        self.assertEqual(target['Arn'], {'Ref': 'AlarmTopic'})
        transformer = target['InputTransformer']
        template = _sub(transformer['InputTemplate'])
        self.assertTrue(template.startswith('"') and template.endswith('"'), template)
        self.assertEqual(template.count('"'), 2)
        for var in re.findall(r'<(\w+)>', template):
            self.assertIn(var, transformer['InputPathsMap'])
        self.assertIn(GROUP, template)
        self.assertIn('A reboot of the primary empties the broker', template)

    def test_the_rule_name_is_inside_every_scope_that_admits_it(self):
        name = _sub(self.rule['Name'])
        self.assertTrue(name.startswith(STACK + '-'))
        self.assertLessEqual(len(name.replace('shop', 'x' * 28)), 64)
        arn = 'arn:aws:events:{}:{}:rule/{}'.format(REGION, ACCOUNT, name)
        # The KMS key admits this stack's rules only.
        key = self.res['PipelineKey']['Properties']['KeyPolicy']['Statement']
        st = next(s for s in key if 'events.amazonaws.com' in _as_list(s.get('Principal', {}).get('Service')))
        allowed = [_sub(a) for a in _as_list(st['Condition']['ArnLike']['aws:SourceArn'])]
        self.assertTrue(any(fnmatch.fnmatchcase(arn, a) for a in allowed))
        self.assertFalse(any(fnmatch.fnmatchcase(arn.replace(STACK, 'other-cdc'), a) for a in allowed))
        self.assertEqual(sorted(st['Action']), ['kms:Decrypt', 'kms:GenerateDataKey*'])
        self.assertEqual(st['Condition']['StringEquals']['aws:SourceAccount'], {'Ref': 'AWS::AccountId'})
        # The deploy role (DeployLinksSupport) may manage it, and no other rule.
        rule_arn = arn.replace(ACCOUNT, '111122223333')
        for action in ('events:PutRule', 'events:PutTargets', 'events:DeleteRule',
                       'events:RemoveTargets', 'events:DescribeRule', 'events:TagResource'):
            self.assertTrue(deploy_role_allows(action, rule_arn), action)
            self.assertFalse(deploy_role_allows(
                action, 'arn:aws:events:us-east-2:111122223333:rule/payments-rule'), action)


class AlarmTopicPolicyTest(unittest.TestCase):
    """A topic policy replaces SNS's default one, so it must keep admitting
    the alarms, or every notification is dropped without an error."""
    def setUp(self):
        self.res = _cdc()['Resources']
        policy = self.res['AlarmTopicPolicy']['Properties']
        self.assertEqual(policy['Topics'], [{'Ref': 'AlarmTopic'}])
        self.statements = policy['PolicyDocument']['Statement']
        self.allowed = [_sub(a) for st in self.statements
                        for a in _as_list(st['Condition']['ArnLike']['aws:SourceArn'])]

    def test_only_cloudwatch_and_eventbridge_publish_from_this_account(self):
        services = sorted(x for st in self.statements for x in _as_list(st['Principal']['Service']))
        self.assertEqual(services, ['cloudwatch.amazonaws.com', 'events.amazonaws.com'])
        for st in self.statements:
            self.assertEqual(st['Action'], 'sns:Publish')
            self.assertEqual(st['Effect'], 'Allow')
            self.assertEqual(st['Resource'], {'Ref': 'AlarmTopic'})
            self.assertEqual(st['Condition']['StringEquals']['aws:SourceAccount'],
                             {'Ref': 'AWS::AccountId'})

    def _admitted(self, arn):
        return any(fnmatch.fnmatchcase(arn, a) for a in self.allowed)

    def test_every_alarm_of_the_stack_may_publish(self):
        alarms = [k for k, v in self.res.items() if v['Type'] == 'AWS::CloudWatch::Alarm'
                  and {'Ref': 'AlarmTopic'} in (v['Properties'].get('AlarmActions') or [])]
        self.assertGreater(len(alarms), 10)
        for logical in alarms:
            name = _sub(self.res[logical]['Properties']['AlarmName'])
            self.assertTrue(self._admitted(
                'arn:aws:cloudwatch:{}:{}:alarm:{}'.format(REGION, ACCOUNT, name)), logical)

    def test_the_rule_may_publish_and_nothing_else(self):
        name = _sub(self.res['BrokerEventsRule']['Properties']['Name'])
        self.assertTrue(self._admitted('arn:aws:events:{}:{}:rule/{}'.format(REGION, ACCOUNT, name)))
        self.assertFalse(self._admitted('arn:aws:events:{}:{}:rule/other'.format(REGION, ACCOUNT)))
        self.assertFalse(self._admitted('arn:aws:cloudwatch:{}:{}:alarm:other-cdc-x'.format(
            REGION, ACCOUNT)))

    def test_policy_exists_whenever_the_topic_does(self):
        self.assertEqual(self.res['AlarmTopicPolicy']['Condition'],
                         self.res['AlarmTopic']['Condition'])


class DocsTest(unittest.TestCase):
    def test_operations_lists_the_new_alarms(self):
        with open(os.path.join(ROOT, 'docs', 'operations.md')) as handle:
            text = handle.read()
        for name in ('-broker-replication-lag', '-broker-failover', '-broker-writes-held',
                     'broker-events'):
            self.assertTrue(name in text, name)


if __name__ == '__main__':
    unittest.main()
