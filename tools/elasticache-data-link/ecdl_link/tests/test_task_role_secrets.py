"""Jira story F171: what a bounded pipeline task role can read, and who can assume it.

TaskRoleBoundary used to allow GetSecretValue on every secret in the account
when PipelineSecretArns was empty. It now allows ECDL's own secrets and the
secrets tagged ecdl:link (with or without PipelineSecretArns), and the deploy preflight warns about a link secret
that is neither listed nor tagged. IAM has no condition key that constrains a
new role's trust policy, so the trust is checked here instead: every role the
link templates create trusts only ECS tasks, and every PassRole the control
plane holds is limited to the services it needs.
"""
import fnmatch
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ecdl_link.preflight import (PIPELINE_ARNS_ENV, PIPELINE_READ_TAG, WARN,  # noqa: E402
                                 pipeline_secret_list, secret_findings)
from ecdl_link.resolve import confirm_secret  # noqa: E402
from ecdl_link.tests.policy_render import WORST_CASE, render  # noqa: E402
from ecdl_link.safe_yaml import load_cfn  # noqa: E402
from ecdl_link.tests.test_control_plane_iam import LINK_TEMPLATES, _template  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VALUES = dict(WORST_CASE, **{'AWS::Partition': 'aws', 'AWS::Region': 'us-east-2',
                             'AWS::AccountId': '111122223333', 'AWS::StackName': 'cp'})


def _secret(name):
    return 'arn:aws:secretsmanager:us-east-2:111122223333:secret:{}-AbCdEf'.format(name)


def _as_list(value):
    return [value] if isinstance(value, str) else list(value or [])


def _conditions_hold(condition, tags):
    """The condition operators the boundary uses, against a secret's tags."""
    for op, checks in (condition or {}).items():
        for key, want in checks.items():
            if not key.startswith('aws:ResourceTag/'):
                raise ValueError('unexpected condition key ' + key)
            present = key[len('aws:ResourceTag/'):] in tags
            if op == 'Null':
                if (str(want).lower() == 'true') == present:
                    return False
            else:
                raise ValueError('unexpected condition operator ' + op)
    return True


def boundary_allows(arn, tags=(), listed=()):
    """Whether TaskRoleBoundary allows GetSecretValue on this secret."""
    resources = _template()['Resources']
    values = dict(VALUES, PipelineSecretArns=list(listed))
    doc = render(resources, 'TaskRoleBoundary', values,
                 conditions={'HasPipelineSecretArns': bool(listed)})
    for st in doc['Statement']:
        if st['Effect'] != 'Allow':
            continue
        if not any(fnmatch.fnmatchcase('secretsmanager:GetSecretValue', a)
                   for a in _as_list(st['Action'])):
            continue
        if not any(fnmatch.fnmatchcase(arn, r) for r in _as_list(st['Resource'])):
            continue
        if _conditions_hold(st.get('Condition'), set(tags)):
            return True
    return False


class BoundarySecretsTest(unittest.TestCase):
    def test_empty_list_admits_only_ecdl_and_tagged_secrets(self):
        self.assertFalse(boundary_allows(_secret('payments-prod-db')))
        self.assertFalse(boundary_allows(_secret('payments-prod-db'), tags={'team'}))
        self.assertTrue(boundary_allows(_secret('shop-db'), tags={PIPELINE_READ_TAG}))
        self.assertTrue(boundary_allows(_secret('ecdl-shop-target')))
        self.assertTrue(boundary_allows(_secret('BrokerAuthSecret-x1y2')))

    def test_a_list_admits_exactly_those_and_ecdl_own(self):
        listed = [_secret('shop-db'), _secret('shop-cache')]
        self.assertTrue(boundary_allows(_secret('shop-db'), listed=listed))
        self.assertTrue(boundary_allows(_secret('shop-cache'), listed=listed))
        self.assertTrue(boundary_allows(_secret('BrokerAuthSecret-x1y2'), listed=listed))
        # A tagged secret is admitted with a list too (an RDS-managed secret,
        # rds!..., tagged but not listed used to be refused at task start).
        self.assertTrue(boundary_allows(_secret('rds!cluster-1'), tags={PIPELINE_READ_TAG},
                                        listed=listed))
        self.assertFalse(boundary_allows(_secret('other'), listed=listed))
        self.assertFalse(boundary_allows(_secret('other'), tags={'team'}, listed=listed))

    def test_no_unconditional_read_of_every_secret(self):
        resources = _template()['Resources']
        for has_list in (False, True):
            doc = render(resources, 'TaskRoleBoundary',
                         dict(VALUES, PipelineSecretArns=[_secret('a')] if has_list else []),
                         conditions={'HasPipelineSecretArns': has_list})
            for st in doc['Statement']:
                if 'secretsmanager:GetSecretValue' not in _as_list(st['Action']):
                    continue
                for r in _as_list(st['Resource']):
                    if r == '*' or r.endswith(':secret:*'):
                        self.assertIn('Condition', st, (has_list, st))

    def test_the_tag_is_the_one_the_preflight_checks(self):
        boundary = _template()['Resources']['TaskRoleBoundary']['Properties']
        text = yaml.safe_dump(boundary)
        self.assertIn('aws:ResourceTag/' + PIPELINE_READ_TAG, text)

    def test_the_control_plane_passes_the_list_to_the_preflight(self):
        with open(os.path.join(ROOT, 'infra', 'ecdl-control-plane.yaml')) as handle:
            text = handle.read()
        self.assertIn("-e ECDL_PIPELINE_SECRET_ARNS='${PipelineSecretList}'", text)
        self.assertIn("PipelineSecretList: !Join [',', !Ref PipelineSecretArns]", text)


class PreflightTest(unittest.TestCase):
    def _r(self, tags=(), name='shop-db', target=None):
        r = {'source': {'secret': {'arn': _secret(name), 'name': name,
                                   'tag_keys': sorted(tags)}}}
        if target:
            r['target'] = {'secret': target}
        return r

    def test_without_a_boundary_nothing_is_checked(self):
        self.assertEqual(secret_findings(self._r(), None), [])

    def test_an_untagged_secret_warns_with_the_fix(self):
        found = secret_findings(self._r(), [])
        self.assertEqual([(f.level, f.path) for f in found], [(WARN, 'source.secret_arn')])
        self.assertIn('tag-resource --secret-id ' + _secret('shop-db'), found[0].message)
        self.assertIn('Key=ecdl:link', found[0].message)

    def test_a_tagged_or_own_secret_is_fine(self):
        self.assertEqual(secret_findings(self._r(tags={'ecdl:link', 'x'}), []), [])
        self.assertEqual(secret_findings(self._r(name='ecdl-shop-target'), []), [])

    def test_with_a_list_only_the_list_counts(self):
        listed = [_secret('shop-db')]
        self.assertEqual(secret_findings(self._r(), listed), [])
        self.assertEqual(secret_findings(self._r(), ['arn:aws:secretsmanager:*:secret:shop-*']), [])
        # Tagged counts with a list too.
        self.assertEqual(secret_findings(self._r(tags={'ecdl:link'}), [_secret('other')]), [])
        found = secret_findings(self._r(), [_secret('other')])
        self.assertEqual([f.path for f in found], ['source.secret_arn'])
        self.assertIn('PipelineSecretArns', found[0].message)
        self.assertIn('Key=ecdl:link', found[0].message)

    def test_the_target_secret_is_checked_too(self):
        target = {'arn': _secret('cache'), 'name': 'cache', 'tag_keys': []}
        found = secret_findings(self._r(tags={'ecdl:link'}, target=target), [])
        self.assertEqual([f.path for f in found], ['target.secret_arn'])

    def test_the_list_comes_from_the_environment(self):
        self.assertEqual(PIPELINE_ARNS_ENV, 'ECDL_PIPELINE_SECRET_ARNS')
        self.assertIsNone(pipeline_secret_list({}))
        self.assertEqual(pipeline_secret_list({PIPELINE_ARNS_ENV: ''}), [])
        self.assertEqual(pipeline_secret_list({PIPELINE_ARNS_ENV: ' a, ,b '}), ['a', 'b'])

    def test_resolve_reports_the_tag_keys_and_never_reads_the_value(self):
        class Secrets(object):
            def describe_secret(self, SecretId=None):
                return {'ARN': SecretId, 'Name': 'shop-db',
                        'Tags': [{'Key': 'ecdl:link', 'Value': 'shop'}, {'Key': 'team'}]}

            def get_secret_value(self, **kw):
                raise AssertionError('must not read the value')

        class Clients(object):
            secretsmanager = Secrets()
        got = confirm_secret(Clients(), _secret('shop-db'), 'source')
        self.assertEqual(got['tag_keys'], ['ecdl:link', 'team'])
        self.assertNotIn('shop', str(got['tag_keys']))


# ── Trust and PassRole ───────────────────────────────────────────────────────
def _load(name):
    with open(os.path.join(ROOT, 'infra', name)) as handle:
        return load_cfn(handle)


class TrustTest(unittest.TestCase):
    def test_every_pipeline_role_trusts_only_ecs_tasks(self):
        # Except the CDC stack's restart function (Jira story F134), which is a Lambda.
        lambda_roles = {('ecdl-cdc-ecs.yaml', 'RestartRole')}
        roles = 0
        for name in LINK_TEMPLATES:
            for logical, res in (_load(name).get('Resources') or {}).items():
                if res['Type'] != 'AWS::IAM::Role':
                    continue
                roles += 1
                props = res['Properties']
                service = ('lambda.amazonaws.com' if (name, logical) in lambda_roles
                           else 'ecs-tasks.amazonaws.com')
                for st in props['AssumeRolePolicyDocument']['Statement']:
                    where = '{} {}'.format(name, logical)
                    self.assertEqual(st['Effect'], 'Allow', where)
                    self.assertEqual(st['Principal'], {'Service': service}, where)
                    self.assertEqual(st['Action'], 'sts:AssumeRole', where)
                    self.assertNotIn('NotPrincipal', st, where)
                # And every one carries the boundary when the control plane sets it.
                self.assertEqual(props.get('PermissionsBoundary'),
                                 {'If': ['HasPermissionsBoundary', {'Ref': 'PermissionsBoundaryArn'},
                                         {'Ref': 'AWS::NoValue'}]}, logical)
        self.assertGreaterEqual(roles, 4)

    def test_link_templates_create_no_other_identities(self):
        for name in LINK_TEMPLATES:
            kinds = {r['Type'] for r in (_load(name).get('Resources') or {}).values()}
            for kind in ('AWS::IAM::User', 'AWS::IAM::Group', 'AWS::IAM::InstanceProfile',
                         'AWS::IAM::Policy', 'AWS::IAM::AccessKey', 'AWS::IAM::OIDCProvider'):
                self.assertNotIn(kind, kinds, name)


def _statements(role_logical):
    resources = _template()['Resources']
    role = resources[role_logical]['Properties']
    for ref in role['ManagedPolicyArns']:
        if isinstance(ref, dict) and 'If' in ref:
            ref = ref['If'][1]
        if not (isinstance(ref, dict) and 'Ref' in ref):
            continue  # an AWS managed policy by ARN
        for st in render(resources, ref['Ref'], VALUES)['Statement']:
            yield ref['Ref'], st


class PassRoleTest(unittest.TestCase):
    def _pass_role_grants(self, role):
        out = []
        for policy, st in _statements(role):
            if st['Effect'] != 'Allow':
                continue
            actions = _as_list(st.get('Action'))
            self.assertNotIn('*', actions, policy)
            self.assertNotIn('iam:*', actions, policy)
            if any(fnmatch.fnmatchcase('iam:PassRole', a) for a in actions):
                out.append(st)
        return out

    def test_the_deploy_role_passes_pipeline_roles_to_ecs_tasks_and_lambda_only(self):
        # Lambda for the CDC stack's restart function (Jira story F134).
        grants = self._pass_role_grants('DeployRole')
        self.assertEqual(len(grants), 1)
        st = grants[0]
        self.assertEqual(st['Condition'], {'StringEquals': {'iam:PassedToService': [
            'ecs-tasks.amazonaws.com', 'lambda.amazonaws.com']}})
        self.assertEqual(_as_list(st['Resource']),
                         ['arn:aws:iam::111122223333:role/ecdl-*'])

    def test_the_instance_passes_only_the_deploy_role_and_pipeline_roles(self):
        grants = self._pass_role_grants('InstanceRole')
        seen = {}
        for st in grants:
            service = st['Condition']['StringEquals']['iam:PassedToService']
            seen[service] = _as_list(st['Resource'])
        self.assertEqual(seen, {
            'cloudformation.amazonaws.com':
                ['arn:aws:iam::111122223333:role/ECDLDeploy-cp-us-east-2'],
            'ecs-tasks.amazonaws.com': ['arn:aws:iam::111122223333:role/ecdl-*'],
        })

    def test_new_task_roles_require_the_boundary(self):
        grants = [st for _p, st in _statements('DeployRole')
                  if st['Effect'] == 'Allow' and 'iam:CreateRole' in _as_list(st['Action'])]
        self.assertEqual(len(grants), 1)
        self.assertIn('iam:PermissionsBoundary', grants[0]['Condition']['StringEquals'])
        self.assertIn(':policy/ECDLPipelineTaskBoundary-cp',
                      grants[0]['Condition']['StringEquals']['iam:PermissionsBoundary'])


if __name__ == '__main__':
    unittest.main()
