"""Jira story F177: a task can decrypt the secrets it is given, and only those.

ECS fetches a task's secrets with the execution role. A secret encrypted with a
customer managed key also needs kms:Decrypt on that key, or the task fails to
start. The grant is through Secrets Manager only (kms:ViaService) and only for
the task's own secrets (Secrets Manager puts the secret's ARN in the KMS
encryption context), so it does not name the keys, and it does not let the
role decrypt anything else with them.
"""
import os
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from ecdl_link.tests.policy_eval import allows  # noqa: E402
from ecdl_link.tests.policy_render import render  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import _CfnLoader  # noqa: E402

REGION, ACCOUNT = 'us-east-2', '111122223333'
KEY = 'arn:aws:kms:{}:{}:key/1234abcd-12ab-34cd-56ef-1234567890ab'.format(REGION, ACCOUNT)
VIA = 'secretsmanager.{}.amazonaws.com'.format(REGION)


def _secret(name):
    return 'arn:aws:secretsmanager:{}:{}:secret:{}-AbCdEf'.format(REGION, ACCOUNT, name)


DB, TARGET, BROKER = _secret('shop-db'), _secret('shop-target'), _secret('BrokerAuthSecret-x1')


def _policy(template, target=True, broker=True):
    with open(os.path.join(ROOT, 'infra', template)) as handle:
        resources = yaml.load(handle, Loader=_CfnLoader)['Resources']  # nosec B506
    values = {'AWS::Partition': 'aws', 'AWS::Region': REGION, 'AWS::AccountId': ACCOUNT,
              'AWS::StackName': 'ecdl-shop-cdc', 'DBSecretArn': DB,
              'TargetSecretArn': TARGET if target else '',
              'BrokerSecretArn': BROKER if broker else '', 'DBSecretJsonKey': ''}
    doc = render(resources, 'ExecutionRolePolicy', values,
                 conditions={'HasTargetSecret': target, 'HasBrokerSecret': broker})
    return doc['Statement']


def can_decrypt(statements, secret, via=VIA, key=KEY):
    context = {'kms:EncryptionContext:SecretARN': secret}
    if via:
        context['kms:ViaService'] = via
    return allows(statements, 'kms:Decrypt', key, context)


class CdcExecutionRoleTest(unittest.TestCase):
    def test_every_given_secret_can_be_decrypted(self):
        st = _policy('ecdl-cdc-ecs.yaml')
        for secret in (DB, TARGET, BROKER):
            self.assertTrue(can_decrypt(st, secret), secret)
            self.assertTrue(allows(st, 'secretsmanager:GetSecretValue', secret), secret)

    def test_only_through_secrets_manager_and_only_these_secrets(self):
        st = _policy('ecdl-cdc-ecs.yaml')
        self.assertFalse(can_decrypt(st, DB, via=None))
        self.assertFalse(can_decrypt(st, DB, via='s3.{}.amazonaws.com'.format(REGION)))
        self.assertFalse(can_decrypt(st, _secret('payments-prod')))
        self.assertFalse(allows(st, 'kms:Decrypt', KEY, {'kms:ViaService': VIA}))
        # A key in another account is not covered by this account's key ARN.
        other = KEY.replace(ACCOUNT, '999988887777')
        self.assertFalse(can_decrypt(st, DB, key=other))

    def test_a_secret_not_given_is_not_admitted(self):
        st = _policy('ecdl-cdc-ecs.yaml', target=False, broker=False)
        self.assertTrue(can_decrypt(st, DB))
        self.assertFalse(can_decrypt(st, TARGET))
        self.assertFalse(can_decrypt(st, BROKER))
        # The empty parameters never become a wildcard or an empty value.
        for s in st:
            cond = (s.get('Condition') or {}).get('StringEquals') or {}
            for value in cond.get('kms:EncryptionContext:SecretARN', []):
                self.assertTrue(value.startswith('arn:aws:secretsmanager:'), value)


class LoaderExecutionRoleTest(unittest.TestCase):
    def test_source_and_target_secrets(self):
        st = _policy('ecdl-loader-ecs.yaml')
        self.assertTrue(can_decrypt(st, DB))
        self.assertTrue(can_decrypt(st, TARGET))
        self.assertFalse(can_decrypt(st, BROKER))
        self.assertFalse(can_decrypt(st, DB, via=None))

    def test_without_a_target_secret(self):
        st = _policy('ecdl-loader-ecs.yaml', target=False)
        self.assertTrue(can_decrypt(st, DB))
        self.assertFalse(can_decrypt(st, TARGET))


class BoundaryAllowsItTest(unittest.TestCase):
    def test_the_task_role_boundary_admits_decrypt_through_secrets_manager(self):
        from ecdl_link.tests.test_control_plane_iam import _template
        from ecdl_link.tests.test_task_role_secrets import VALUES
        doc = render(_template()['Resources'], 'TaskRoleBoundary', VALUES,
                     conditions={'HasPipelineSecretArns': False})
        self.assertTrue(can_decrypt(doc['Statement'], DB))
        self.assertFalse(can_decrypt(doc['Statement'], DB, via=None))


if __name__ == '__main__':
    unittest.main()
