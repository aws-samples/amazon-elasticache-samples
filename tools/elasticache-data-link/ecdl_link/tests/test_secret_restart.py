"""Jira story F134: a service restarts when its secret gets a new value.

ECS injects secrets only when a task starts, so a rotated source password
(RDS-managed secrets rotate on their own) left Debezium on the old one until
its next reconnect failed. The CDC stack now has an EventBridge rule on
Secrets Manager's "Secret Label Updated" event for AWSCURRENT, and a small
function that forces a new deployment of the service that reads that secret.

The function's code is run here against a fake ECS client, with the values
CloudFormation's !Sub would put in, so the tests check what it does, not only
what the YAML says.
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link.tests.policy_eval import allows  # noqa: E402
from ecdl_link.safe_yaml import load_cfn  # noqa: E402
from ecdl_link.tests.test_control_plane_least_privilege import _arn, _statements  # noqa: E402

CDC = os.path.join(ROOT, 'infra', 'ecdl-cdc-ecs.yaml')
STACK, CLUSTER = 'ecdl-shop-cdc', 'ecdl-shop'
SOURCE = 'arn:aws:secretsmanager:us-east-2:111122223333:secret:rds!cluster-1a2b-AbCdEf'
TARGET = 'arn:aws:secretsmanager:us-east-2:111122223333:secret:ecdl-shop-target-XyZ123'


def _cdc():
    with open(CDC) as handle:
        return load_cfn(handle)


class _FakeEcs:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def update_service(self, **kwargs):
        if self.fail:
            raise self.fail
        self.calls.append(kwargs)


def _module(name, code):
    """The template's inline Lambda code as a module, the way Lambda loads index.py."""
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, name + '.py')
    with open(path, 'w') as handle:
        handle.write(code)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
        shutil.rmtree(folder, ignore_errors=True)
    return module


# TargetSecretArn of a target without auth.
NO_TARGET_AUTH = ''


def run_function(event, target_secret=TARGET, ecs=None, log=None):
    """Run RestartFunction's code as Lambda would, with a fake boto3.
    Returns the UpdateService calls; what it prints goes to `log`."""
    code = _cdc()['Resources']['RestartFunction']['Properties']['Code']['ZipFile']['Sub']
    names = {'DBSecretArn': SOURCE, 'TargetSecretArn': target_secret,
             'ClusterName': CLUSTER, 'AWS::StackName': STACK}
    unknown = set(re.findall(r'\$\{([^}]+)\}', code)) - set(names)
    if unknown:
        raise AssertionError('the code uses ' + ', '.join(sorted(unknown)))
    code = re.sub(r'\$\{([^}]+)\}', lambda m: names[m.group(1)], code)
    ecs = ecs or _FakeEcs()
    fake = types.ModuleType('boto3')
    fake.client = lambda service: ecs if service == 'ecs' else None
    saved = sys.modules.get('boto3')
    sys.modules['boto3'] = fake
    try:
        scope = _module('index', code).__dict__
        with contextlib.redirect_stdout(log if log is not None else io.StringIO()):
            scope['handler'](event, None)
    finally:
        if saved is None:
            del sys.modules['boto3']
        else:
            sys.modules['boto3'] = saved
    return ecs.calls


def _event(arn):
    return {'source': 'aws.secretsmanager', 'detail-type': 'Secret Label Updated',
            'resources': [arn], 'detail': {'name': 'x', 'labelUpdated': 'AWSCURRENT'}}


class FunctionTest(unittest.TestCase):
    def test_a_new_source_password_restarts_debezium_only(self):
        log = io.StringIO()
        self.assertEqual(run_function(_event(SOURCE), log=log), [
            {'cluster': CLUSTER, 'service': STACK + '-debezium', 'forceNewDeployment': True}])
        # The line operations.md tells the operator to look for.
        self.assertEqual(log.getvalue(), 'restarted debezium for {}\n'.format(SOURCE))

    def test_a_new_target_credential_restarts_the_sink_only(self):
        # Debezium does not read it, and a restart stops capture for minutes.
        self.assertEqual(run_function(_event(TARGET)), [
            {'cluster': CLUSTER, 'service': STACK + '-sink', 'forceNewDeployment': True}])

    def test_another_secret_restarts_nothing(self):
        other = 'arn:aws:secretsmanager:us-east-2:111122223333:secret:payments-AbCdEf'
        self.assertEqual(run_function(_event(other)), [])
        # A name that only starts like ours is another secret.
        self.assertEqual(run_function(_event(SOURCE[:-7])), [])
        self.assertEqual(run_function({}), [])

    def test_no_target_secret_matches_nothing(self):
        # TargetSecretArn is empty for a target without auth: '' must not
        # match an event, even one with no resources or an empty one.
        self.assertEqual(run_function(_event(''), target_secret=NO_TARGET_AUTH), [])
        self.assertEqual(run_function({'resources': []}, target_secret=NO_TARGET_AUTH), [])
        self.assertEqual(len(run_function(_event(SOURCE), target_secret=NO_TARGET_AUTH)), 1)

    def test_one_secret_for_both_restarts_both(self):
        both = run_function(_event(SOURCE), target_secret=SOURCE)
        self.assertEqual([c['service'] for c in both], [STACK + '-debezium', STACK + '-sink'])

    def test_a_failed_restart_fails_the_invocation(self):
        # So Lambda retries it and the error is in the function's log.
        with self.assertRaises(RuntimeError):
            run_function(_event(SOURCE), ecs=_FakeEcs(fail=RuntimeError('AccessDenied')))


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.res = _cdc()['Resources']
        self.rule = self.res['SecretChangedRule']['Properties']

    def test_matches_a_new_current_value_of_the_links_secrets(self):
        pattern = self.rule['EventPattern']
        self.assertEqual(pattern['source'], ['aws.secretsmanager'])
        self.assertEqual(pattern['detail-type'], ['Secret Label Updated'])
        self.assertEqual(pattern['detail'], {'labelUpdated': ['AWSCURRENT']})
        self.assertEqual(pattern['resources'], [
            {'Ref': 'DBSecretArn'},
            {'If': ['HasTargetSecret', {'Ref': 'TargetSecretArn'}, {'Ref': 'DBSecretArn'}]}])

    def test_it_exists_whatever_the_alarm_settings(self):
        # A restart is not an alarm: links with alarms off need it too.
        for logical in ('SecretChangedRule', 'RestartFunction', 'RestartPermission',
                        'RestartRole', 'RestartPolicy', 'RestartLogGroup'):
            self.assertNotIn('Condition', self.res[logical], logical)

    def test_only_this_rule_may_invoke_the_function(self):
        self.assertEqual(self.rule['Targets'], [
            {'Id': 'restart', 'Arn': {'GetAtt': 'RestartFunction.Arn'}}])
        perm = self.res['RestartPermission']['Properties']
        self.assertEqual(perm, {'FunctionName': {'Ref': 'RestartFunction'},
                                'Action': 'lambda:InvokeFunction',
                                'Principal': 'events.amazonaws.com',
                                'SourceArn': {'GetAtt': 'SecretChangedRule.Arn'}})


class RoleTest(unittest.TestCase):
    def setUp(self):
        self.res = _cdc()['Resources']

    def test_the_role_may_only_restart_these_two_services_and_log(self):
        statements = self.res['RestartPolicy']['Properties']['PolicyDocument']['Statement']
        self.assertEqual(statements, [
            {'Effect': 'Allow', 'Action': 'ecs:UpdateService',
             'Resource': [{'Ref': 'DebeziumService'}, {'Ref': 'SinkService'}]},
            {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],
             'Resource': {'GetAtt': 'RestartLogGroup.Arn'}}])

    def test_the_role_is_for_lambda_with_the_boundary(self):
        role = self.res['RestartRole']['Properties']
        self.assertEqual(role['AssumeRolePolicyDocument']['Statement'], [
            {'Effect': 'Allow', 'Principal': {'Service': 'lambda.amazonaws.com'},
             'Action': 'sts:AssumeRole'}])
        self.assertEqual(role['ManagedPolicyArns'], [{'Ref': 'RestartPolicy'}])
        self.assertEqual(role['PermissionsBoundary'], self.res['TaskRole']['Properties']['PermissionsBoundary'])

    def test_the_function_logs_to_its_own_encrypted_group(self):
        fn = self.res['RestartFunction']['Properties']
        self.assertEqual(fn['Role'], {'GetAtt': 'RestartRole.Arn'})
        self.assertEqual(fn['LoggingConfig'], {'LogGroup': {'Ref': 'RestartLogGroup'}})
        self.assertNotIn('Environment', fn)
        self.assertGreaterEqual(fn['Timeout'], 10)
        group = self.res['RestartLogGroup']['Properties']
        self.assertEqual(group['LogGroupName'], {'Sub': '/ecs/${AWS::StackName}/restart'})
        self.assertEqual(group['KmsKeyId'], {'GetAtt': 'PipelineKey.Arn'})
        self.assertEqual(group['RetentionInDays'],
                         self.res['SinkLogGroup']['Properties']['RetentionInDays'])
        # The key admits CloudWatch Logs for /ecs/<stack>/* only.
        key = self.res['PipelineKey']['Properties']['KeyPolicy']['Statement']
        logs = next(s for s in key if s.get('Sid') == 'CloudWatchLogsForThisStacksLogGroups')
        self.assertEqual(logs['Condition']['ArnLike']['kms:EncryptionContext:aws:logs:arn']['Sub'],
                         'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:'
                         '/ecs/${AWS::StackName}/*')

    def test_the_scanner_exceptions_are_only_the_three_explained(self):
        meta = self.res['RestartFunction']['Metadata']
        self.assertEqual(sorted(s['id'] for s in meta['checkov']['skip']),
                         ['CKV_AWS_115', 'CKV_AWS_116', 'CKV_AWS_117'])
        self.assertEqual(sorted(meta['guard']['SuppressedRules']),
                         ['LAMBDA_CONCURRENCY_CHECK', 'LAMBDA_DLQ_CHECK', 'LAMBDA_INSIDE_VPC'])


class ControlPlaneTest(unittest.TestCase):
    """The control plane can deploy it, and the boundary lets it work."""
    FUNCTION = _arn('lambda', 'function:ecdl-shop-cdc-RestartFunction-1A2B3C4D')
    ROLE = 'arn:aws:iam::111122223333:role/ecdl-shop-cdc-RestartRole-1A2B3C4D'

    def setUp(self):
        self.deploy = _statements('DeployRole')

    def test_the_deploy_role_manages_link_functions_only(self):
        for action in ('lambda:CreateFunction', 'lambda:DeleteFunction', 'lambda:GetFunction',
                       'lambda:UpdateFunctionCode', 'lambda:UpdateFunctionConfiguration',
                       'lambda:AddPermission', 'lambda:RemovePermission', 'lambda:TagResource'):
            self.assertTrue(allows(self.deploy, action, self.FUNCTION), action)
            self.assertFalse(allows(self.deploy, action, _arn('lambda', 'function:payments-api')), action)
        # Never invoke one, nor change who may.
        self.assertFalse(allows(self.deploy, 'lambda:InvokeFunction', self.FUNCTION))
        self.assertFalse(allows(self.deploy, 'lambda:PutFunctionUrlConfig', self.FUNCTION))

    def test_the_deploy_role_passes_link_roles_to_lambda(self):
        ctx = {'iam:PassedToService': 'lambda.amazonaws.com'}
        self.assertTrue(allows(self.deploy, 'iam:PassRole', self.ROLE, ctx))
        self.assertFalse(allows(self.deploy, 'iam:PassRole',
                                'arn:aws:iam::111122223333:role/Admin', ctx))
        self.assertFalse(allows(self.deploy, 'iam:PassRole', self.ROLE,
                                {'iam:PassedToService': 'ec2.amazonaws.com'}))

    def test_the_boundary_admits_the_restart_and_its_log(self):
        from ecdl_link.tests.policy_render import render
        from ecdl_link.tests.test_control_plane_iam import _template
        from ecdl_link.tests.test_control_plane_least_privilege import VALUES
        boundary = render(_template()['Resources'], 'TaskRoleBoundary', VALUES)['Statement']
        service = _arn('ecs', 'service/ecdl-shop/ecdl-shop-cdc-debezium')
        self.assertTrue(allows(boundary, 'ecs:UpdateService', service))
        self.assertFalse(allows(boundary, 'ecs:UpdateService', _arn('ecs', 'service/prod/payments')))
        self.assertFalse(allows(boundary, 'ecs:DeleteService', service))
        group = _arn('logs', 'log-group:/ecs/ecdl-shop-cdc/restart:*')
        self.assertTrue(allows(boundary, 'logs:PutLogEvents', group))

    def test_teardown_keeps_its_log_lines_too(self):
        from ecdl_link.teardown import log_groups_of
        self.assertIn('/ecs/ecdl-shop-cdc/restart', log_groups_of('cdc', 'ecdl-shop-cdc'))


if __name__ == '__main__':
    unittest.main()
