"""Least privilege of the control plane, by policy evaluation.

Jira story F172  the deploy role changes only link alarms (and their target tracking).
Jira story F173  no role may write a CloudWatch Logs resource policy; one, in the
      control-plane stack, admits log delivery to the link log groups.
Jira story F175  the instance reads no SSM parameters (AmazonSSMManagedInstanceCore
      without GetParameter/GetParameters).
Jira story F176  the instance reads a BrokerAuthSecret-* secret only when an ECDL stack's
      tag is on it.
Jira story F105  the instance may list an alarm topic's subscriptions (link topics only).
Jira story F100  the instance writes its own log group; the deploy role cannot remove it
      or the recovery alarm.

Each policy is rendered as IAM sees it (policy_render) and evaluated with
policy_eval, so the tests check what is allowed, not how the YAML reads.
"""
import json
import os
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link.tests.policy_eval import allows  # noqa: E402
from ecdl_link.tests.policy_render import render  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import _CfnLoader, _template  # noqa: E402

REGION, ACCOUNT = 'us-east-2', '111122223333'
VALUES = {'AWS::Partition': 'aws', 'AWS::Region': REGION, 'AWS::AccountId': ACCOUNT,
          'AWS::StackName': 'cp', 'AWS::URLSuffix': 'amazonaws.com',
          'ManagedResourcePrefix': 'ecdl', 'TemplateBucket': 'ecdl-templates',
          'DeployVpcId': '', 'PipelineSecretArns': [], 'MonitorSourceSecretArns': []}


def _statements(role, conditions=None):
    """Every statement of a role's customer managed policies, rendered."""
    resources = _template()['Resources']
    out = []
    for ref in resources[role]['Properties']['ManagedPolicyArns']:
        if isinstance(ref, dict) and 'If' in ref:
            ref = ref['If'][1]
        if not (isinstance(ref, dict) and 'Ref' in ref):
            continue                       # an AWS managed policy ARN
        doc = render(resources, ref['Ref'], VALUES, conditions or {})
        out.extend(doc['Statement'])
    return out


def _arn(service, resource):
    return 'arn:aws:{}:{}:{}:{}'.format(service, REGION, ACCOUNT, resource)


def _alarm(name):
    return _arn('cloudwatch', 'alarm:' + name)


class DeployRoleAlarmsTest(unittest.TestCase):  # Jira story F172
    def setUp(self):
        self.st = _statements('DeployRole')

    def test_link_and_target_tracking_alarms_only(self):
        ok = ['ecdl-shop-cdc-sink-lag', 'ecdl-shop-cdc-dead-letters',
              'TargetTracking-service/ecdl-shop/ecdl-shop-cdc-sink-AlarmHigh-0a1b2c3d',
              'TargetTracking-service/ecdl-shop/ecdl-shop-cdc-sink-AlarmLow-0a1b2c3d']
        no = ['payments-prod-cpu', 'ECDLControlPlane-cp-system-check',
              'TargetTracking-service/payments/payments-api-AlarmHigh-1',
              'TargetTracking-table/ecdl-x-AlarmHigh-1', 'Ecdl-shop-cdc-sink-lag']
        for action in ('cloudwatch:PutMetricAlarm', 'cloudwatch:DeleteAlarms',
                       'cloudwatch:TagResource'):
            for name in ok:
                self.assertTrue(allows(self.st, action, _alarm(name)), (action, name))
            for name in no:
                self.assertFalse(allows(self.st, action, _alarm(name)), (action, name))

    def test_describe_stays_readable(self):
        self.assertTrue(allows(self.st, 'cloudwatch:DescribeAlarms', _alarm('anything')))

    def test_every_alarm_the_cdc_stack_creates_is_in_scope(self):
        with open(os.path.join(ROOT, 'infra', 'ecdl-cdc-ecs.yaml')) as handle:
            cdc = yaml.load(handle, Loader=_CfnLoader)  # nosec B506
        names = [r['Properties']['AlarmName']['Sub'].replace('${AWS::StackName}', 'ecdl-shop-cdc')
                 for r in cdc['Resources'].values() if r['Type'] == 'AWS::CloudWatch::Alarm']
        self.assertGreater(len(names), 10)
        for name in names:
            self.assertTrue(allows(self.st, 'cloudwatch:PutMetricAlarm', _alarm(name)), name)
        # Target tracking names its alarms after the scalable target's resource
        # id, which is in the link's cluster (ecdl-<link>).
        from ecdl_link.provision import cluster_name
        self.assertTrue(cluster_name('shop', 'ecdl').startswith('ecdl-'))


class LogsResourcePolicyTest(unittest.TestCase):  # Jira story F173
    def test_no_role_writes_a_logs_resource_policy(self):
        policy = _arn('logs', 'destination:x')
        here = {'aws:RequestedRegion': REGION}
        for role in ('DeployRole', 'InstanceRole'):
            st = _statements(role)
            for action in ('logs:PutResourcePolicy', 'logs:DeleteResourcePolicy'):
                for resource in ('*', policy):
                    self.assertFalse(allows(st, action, resource, here), (role, action))
        boundary = render(_template()['Resources'], 'TaskRoleBoundary', VALUES,
                          {'HasPipelineSecretArns': False})['Statement']
        self.assertFalse(allows(boundary, 'logs:PutResourcePolicy', '*'))

    def test_the_deploy_role_still_creates_log_deliveries(self):
        st = _statements('DeployRole')
        for action in ('logs:CreateLogDelivery', 'logs:GetLogDelivery', 'logs:DeleteLogDelivery',
                       'logs:DescribeResourcePolicies'):
            self.assertTrue(allows(st, action, '*', {'aws:RequestedRegion': REGION}), action)

    def _delivery_policy(self):
        res = _template()['Resources']['LogDeliveryPolicy']
        self.assertEqual(res['Type'], 'AWS::Logs::ResourcePolicy')
        self.assertEqual(res.get('DeletionPolicy'), 'Retain')
        text = res['Properties']['PolicyDocument']['Sub']
        for key, value in (('AWS::Partition', 'aws'), ('AWS::Region', REGION),
                           ('AWS::AccountId', ACCOUNT), ('ManagedResourcePrefix', 'ecdl')):
            text = text.replace('${%s}' % key, value)
        return json.loads(text)

    def test_the_stack_admits_log_delivery_to_link_log_groups_only(self):
        doc = self._delivery_policy()
        st = doc['Statement']
        self.assertEqual([s['Principal'] for s in st], [{'Service': 'delivery.logs.amazonaws.com'}])
        ctx = {'aws:SourceAccount': ACCOUNT, 'aws:SourceArn': _arn('logs', 'delivery-source:x')}
        broker = _arn('logs', 'log-group:/ecs/ecdl-shop-broker/broker:log-stream:abc')
        self.assertTrue(allows(st, 'logs:PutLogEvents', broker, ctx))
        self.assertTrue(allows(st, 'logs:CreateLogStream', broker, ctx))
        self.assertFalse(allows(st, 'logs:PutLogEvents', _arn('logs', 'log-group:/aws/lambda/x:log-stream:y'), ctx))
        self.assertFalse(allows(st, 'logs:PutLogEvents', broker, dict(ctx, **{'aws:SourceAccount': '999988887777'})))
        self.assertFalse(allows(st, 'logs:DeleteLogGroup', broker, ctx))

    def test_the_broker_log_group_is_inside_that_scope(self):
        with open(os.path.join(ROOT, 'infra', 'ecdl-broker-quickstart.yaml')) as handle:
            broker = yaml.load(handle, Loader=_CfnLoader)  # nosec B506
        name = broker['Resources']['BrokerLogGroup']['Properties']['LogGroupName']['Sub']
        name = name.replace('${AWS::StackName}', 'ecdl-shop-broker')
        st = self._delivery_policy()['Statement']
        ctx = {'aws:SourceAccount': ACCOUNT, 'aws:SourceArn': _arn('logs', 'x')}
        self.assertTrue(allows(st, 'logs:PutLogEvents', _arn('logs', 'log-group:{}:log-stream:s'.format(name)), ctx))


# AmazonSSMManagedInstanceCore, as published by AWS.
SSM_CORE = {
    'ssm:DescribeAssociation', 'ssm:GetDeployablePatchSnapshotForInstance', 'ssm:GetDocument',
    'ssm:DescribeDocument', 'ssm:GetManifest', 'ssm:GetParameter', 'ssm:GetParameters',
    'ssm:ListAssociations', 'ssm:ListInstanceAssociations', 'ssm:PutInventory',
    'ssm:PutComplianceItems', 'ssm:PutConfigurePackageResult', 'ssm:UpdateAssociationStatus',
    'ssm:UpdateInstanceAssociationStatus', 'ssm:UpdateInstanceInformation',
    'ssmmessages:CreateControlChannel', 'ssmmessages:CreateDataChannel',
    'ssmmessages:OpenControlChannel', 'ssmmessages:OpenDataChannel',
    'ec2messages:AcknowledgeMessage', 'ec2messages:DeleteMessage', 'ec2messages:FailMessage',
    'ec2messages:GetEndpoint', 'ec2messages:GetMessages', 'ec2messages:SendReply'}


class SessionManagerTest(unittest.TestCase):  # Jira story F175
    def test_the_aws_managed_policy_is_gone(self):
        arns = _template()['Resources']['InstanceRole']['Properties']['ManagedPolicyArns']
        self.assertFalse([a for a in arns if isinstance(a, str) and 'SSMManagedInstanceCore' in a])

    def test_no_parameter_can_be_read(self):
        st = _statements('InstanceRole')
        here = {'aws:RequestedRegion': REGION}
        for name in ('parameter/prod/db/password', 'parameter/aws/service/ami-amazon-linux-latest/x'):
            for action in ('ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'):
                self.assertFalse(allows(st, action, _arn('ssm', name), here), (action, name))

    def test_everything_else_in_the_aws_policy_is_kept(self):
        st = _statements('InstanceRole')
        here = {'aws:RequestedRegion': REGION}
        for action in sorted(SSM_CORE - {'ssm:GetParameter', 'ssm:GetParameters'}):
            self.assertTrue(allows(st, action, '*', here), action)
            self.assertFalse(allows(st, action, '*', {'aws:RequestedRegion': 'eu-west-1'}), action)


def _secret(name):
    return _arn('secretsmanager', 'secret:{}-AbCdEf'.format(name))


class BrokerSecretTagTest(unittest.TestCase):  # Jira story F176
    def setUp(self):
        self.st = _statements('InstanceRole', {'HasMonitorSourceSecrets': False})
        self.secret = _secret('BrokerAuthSecret-x1y2z3')

    def _reads(self, arn, **tags):
        ctx = {'aws:ResourceTag/' + k.replace('__', ':'): v for k, v in tags.items()}
        return allows(self.st, 'secretsmanager:GetSecretValue', arn, ctx)

    def test_an_untagged_or_foreign_broker_secret_is_not_read(self):
        self.assertFalse(self._reads(self.secret))
        self.assertFalse(self._reads(self.secret, ecdl__stack='payments-broker'))
        self.assertFalse(self._reads(self.secret, aws__cloudformation__stack__name='payments-broker'))

    def test_a_broker_secret_of_an_ecdl_stack_is_read(self):
        self.assertTrue(self._reads(self.secret, ecdl__stack='ecdl-shop-broker'))
        # Created before the ecdl:stack tag: CloudFormation's own tag.
        ctx = {'aws:ResourceTag/aws:cloudformation:stack-name': 'ecdl-shop-broker'}
        self.assertTrue(allows(self.st, 'secretsmanager:GetSecretValue', self.secret, ctx))

    def test_ecdl_named_secrets_need_no_tag(self):
        self.assertTrue(self._reads(_secret('ecdl-shop-target')))

    def test_the_broker_template_tags_its_secret(self):
        with open(os.path.join(ROOT, 'infra', 'ecdl-broker-quickstart.yaml')) as handle:
            broker = yaml.load(handle, Loader=_CfnLoader)  # nosec B506
        tags = {t['Key']: t['Value'] for t in
                broker['Resources']['BrokerAuthSecret']['Properties']['Tags']}
        self.assertEqual(tags['ecdl:stack'], {'Ref': 'AWS::StackName'})


class AlarmTopicReadTest(unittest.TestCase):  # Jira story F105
    def test_link_topics_only(self):
        st = _statements('InstanceRole', {'HasMonitorSourceSecrets': False})
        action = 'sns:ListSubscriptionsByTopic'
        self.assertTrue(allows(st, action, _arn('sns', 'ecdl-shop-cdc-AlarmTopic-1A2B3C')))
        self.assertFalse(allows(st, action, _arn('sns', 'payments-alerts')))
        self.assertFalse(allows(st, 'sns:Subscribe', _arn('sns', 'ecdl-shop-cdc-AlarmTopic-1A2B3C')))


class OwnLogsTest(unittest.TestCase):  # Jira story F100
    def setUp(self):
        self.res = _template()['Resources']

    def test_the_instance_writes_its_own_group_only(self):
        st = _statements('InstanceRole', {'HasMonitorSourceSecrets': False})
        group = '/ecdl/control-plane/cp'
        self.assertEqual(self.res['ControlPlaneLogGroup']['Properties']['LogGroupName'],
                         {'Sub': '/ecdl/control-plane/${AWS::StackName}'})
        stream = _arn('logs', 'log-group:{}:log-stream:backend'.format(group))
        self.assertTrue(allows(st, 'logs:PutLogEvents', stream))
        self.assertTrue(allows(st, 'logs:CreateLogStream', stream))
        self.assertTrue(allows(st, 'logs:DescribeLogStreams', _arn('logs', 'log-group:' + group)))
        self.assertFalse(allows(st, 'logs:PutLogEvents', _arn('logs', 'log-group:/aws/x:log-stream:y')))
        self.assertFalse(allows(st, 'logs:DeleteLogGroup', _arn('logs', 'log-group:' + group)))

    def test_the_deploy_role_cannot_remove_it_or_the_recovery_alarm(self):
        st = _statements('DeployRole')
        group = _arn('logs', 'log-group:/ecdl/control-plane/cp')
        self.assertFalse(allows(st, 'logs:DeleteLogGroup', group))
        self.assertFalse(allows(st, 'logs:PutRetentionPolicy', group))
        alarm = _alarm('ECDLControlPlane-cp-system-check')
        self.assertEqual(self.res['InstanceRecoveryAlarm']['Properties']['AlarmName'],
                         {'Sub': 'ECDLControlPlane-${AWS::StackName}-system-check'})
        for action in ('cloudwatch:PutMetricAlarm', 'cloudwatch:DeleteAlarms'):
            self.assertFalse(allows(st, action, alarm), action)

    def test_the_instance_has_one_minute_metrics(self):
        # EC2_INSTANCE_DETAILED_MONITORING_ENABLED (cfn-guard): the recovery
        # alarm and an operator see a failing host within minutes.
        self.assertIs(self.res['Instance']['Properties']['Monitoring'], True)
    def test_the_log_group_is_encrypted_with_a_key_that_admits_only_it(self):
        props = self.res['ControlPlaneLogGroup']['Properties']
        self.assertEqual(props['KmsKeyId'], {'GetAtt': 'ControlPlaneLogKey.Arn'})
        policy = self.res['ControlPlaneLogKey']['Properties']['KeyPolicy']['Statement']
        logs = [s for s in policy if s['Sid'] == 'CloudWatchLogsForThisLogGroup'][0]
        arn = logs['Condition']['ArnEquals']['kms:EncryptionContext:aws:logs:arn']['Sub']
        self.assertTrue(arn.endswith(':log-group:/ecdl/control-plane/${AWS::StackName}'))
        # Not tagged ecdl:stack, so the deploy role's key grants do not reach it.
        self.assertNotIn('Tags', self.res['ControlPlaneLogKey']['Properties'])

    def test_a_failed_system_check_recovers_the_instance(self):
        p = self.res['InstanceRecoveryAlarm']['Properties']
        self.assertEqual((p['Namespace'], p['MetricName']), ('AWS/EC2', 'StatusCheckFailed_System'))
        self.assertEqual(p['Dimensions'], [{'Name': 'InstanceId', 'Value': {'Ref': 'Instance'}}])
        self.assertEqual(p['AlarmActions'],
                         [{'Sub': 'arn:${AWS::Partition}:automate:${AWS::Region}:ec2:recover'}])
        self.assertEqual((p['Threshold'], p['ComparisonOperator']),
                         (1, 'GreaterThanOrEqualToThreshold'))
        from ecdl_link.tests.test_alarm_rules import _ops_anchors
        anchor = p['AlarmDescription']['Sub'].split('docs/operations.md#')[1].split()[0]
        self.assertIn(anchor, _ops_anchors())


if __name__ == '__main__':
    unittest.main()
