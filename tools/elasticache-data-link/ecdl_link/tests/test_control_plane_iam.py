"""The control plane may not change its own role, policies or stack.

Its grants cover IAM roles, policies and stacks named ecdl-*. Its own resources
used to be named after the stack (ecdl-control-plane-...), which put them in
that scope: the instance could CreatePolicyVersion on its own deploy policy and
grant itself anything. These tests keep the control plane's IAM resources named
outside the scope and keep the explicit denies in place.
"""
import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(ROOT, 'infra', 'ecdl-control-plane.yaml')
PREFIX = 'ecdl'


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_CfnLoader.add_multi_constructor('!', _tag)


def _template():
    with open(TEMPLATE) as handle:
        return yaml.load(handle, Loader=_CfnLoader)  # nosec B506 - SafeLoader subclass


def _sub_text(value):
    """The literal text of a !Sub, or None for anything else."""
    if isinstance(value, dict) and 'Sub' in value:
        sub = value['Sub']
        return sub if isinstance(sub, str) else sub[0]
    return None


class ControlPlaneIamTest(unittest.TestCase):
    def setUp(self):
        self.resources = _template()['Resources']

    def _iam(self, kind):
        return {k: v for k, v in self.resources.items() if v['Type'] == kind}

    def test_own_iam_resources_are_named_outside_the_scope(self):
        names = {'AWS::IAM::Role': 'RoleName',
                 'AWS::IAM::ManagedPolicy': 'ManagedPolicyName',
                 'AWS::IAM::InstanceProfile': 'InstanceProfileName'}
        for kind, prop in names.items():
            for logical, res in self._iam(kind).items():
                name = _sub_text(res['Properties'].get(prop))
                self.assertIsNotNone(
                    name, '{} has no explicit {}: CloudFormation would name it after '
                    'the stack, which is ecdl-...'.format(logical, prop))
                # IAM matches ARNs case-sensitively, so only a lower-case
                # "ecdl-" start (or ${AWS::StackName} first) falls in scope.
                self.assertFalse(name.startswith(PREFIX + '-'), logical)
                self.assertFalse(name.startswith('${AWS::StackName}'), logical)

    def test_role_carries_the_self_protection_policy(self):
        role = self.resources['InstanceRole']['Properties']
        self.assertIn({'Ref': 'SelfProtectionPolicy'}, role['ManagedPolicyArns'])

    def test_self_protection_denies_own_resources_and_stack(self):
        statements = (self.resources['SelfProtectionPolicy']['Properties']
                      ['PolicyDocument']['Statement'])
        by_sid = {s['Sid']: s for s in statements}
        own = by_sid['SelfProtectionOwnRoleAndPolicies']
        self.assertEqual(own['Effect'], 'Deny')
        self.assertEqual(sorted(own['NotAction']), ['iam:Get*', 'iam:List*'])
        role_name = _sub_text(self.resources['InstanceRole']['Properties']['RoleName'])
        targets = [_sub_text(r) for r in own['Resource']]
        self.assertTrue(any(t.endswith(':role/' + role_name) for t in targets))
        self.assertTrue(any(':policy/' + role_name + '-*' in t for t in targets))
        stack = by_sid['SelfProtectionOwnStack']
        self.assertEqual(stack['Effect'], 'Deny')
        self.assertEqual(stack['Resource'], {'Ref': 'AWS::StackId'})
        for action in ('cloudformation:UpdateStack', 'cloudformation:DeleteStack',
                       'cloudformation:ExecuteChangeSet'):
            self.assertIn(action, stack['Action'])

    def test_every_own_policy_name_is_covered_by_the_deny(self):
        role_name = _sub_text(self.resources['InstanceRole']['Properties']['RoleName'])
        for logical, res in self._iam('AWS::IAM::ManagedPolicy').items():
            if logical == 'TaskRoleBoundary':
                continue  # denied separately in DeployLinksPolicy (DenyEditingTheBoundary)
            name = _sub_text(res['Properties']['ManagedPolicyName'])
            self.assertTrue(name.startswith(role_name + '-'), logical)


# CloudFormation validates a typed parameter with the caller's (here: the
# deploy role's) permissions before it changes anything.
_PARAMETER_READS = {
    'AWS::EC2::VPC::Id': 'ec2:DescribeVpcs',
    'List<AWS::EC2::VPC::Id>': 'ec2:DescribeVpcs',
    'AWS::EC2::Subnet::Id': 'ec2:DescribeSubnets',
    'List<AWS::EC2::Subnet::Id>': 'ec2:DescribeSubnets',
    'AWS::EC2::SecurityGroup::Id': 'ec2:DescribeSecurityGroups',
    'List<AWS::EC2::SecurityGroup::Id>': 'ec2:DescribeSecurityGroups',
}
LINK_TEMPLATES = ('ecdl-broker-quickstart.yaml', 'ecdl-cdc-ecs.yaml', 'ecdl-loader-ecs.yaml')


class DeployRoleTest(unittest.TestCase):
    """D1: link stacks are built by the deploy role, so it needs every read
    CloudFormation makes on the stack's behalf, not only the writes."""

    def setUp(self):
        self.resources = _template()['Resources']
        role = self.resources['DeployRole']['Properties']
        self.actions = set()
        for ref in role['ManagedPolicyArns']:
            doc = self.resources[ref['Ref']]['Properties']['PolicyDocument']
            for st in doc['Statement']:
                if st['Effect'] == 'Allow':
                    acts = st.get('Action', [])
                    self.actions.update([acts] if isinstance(acts, str) else acts)

    def test_every_typed_parameter_can_be_validated(self):
        for name in LINK_TEMPLATES:
            with open(os.path.join(ROOT, 'infra', name)) as handle:
                params = yaml.load(handle, Loader=_CfnLoader).get('Parameters') or {}  # nosec B506
            for pname, p in params.items():
                need = _PARAMETER_READS.get(p.get('Type'))
                if need:
                    self.assertIn(need, self.actions, '{} {}'.format(name, pname))
                else:
                    self.assertNotIn('AWS::', str(p.get('Type')),
                                     '{} {}: map its Type to the read it needs'.format(name, pname))

    def test_resource_handlers_can_read_back_what_they_create(self):
        for action in ('elasticache:DescribeReplicationGroups',
                       'elasticache:DescribeCacheParameters',
                       'logs:DescribeLogGroups', 'secretsmanager:DescribeSecret',
                       'ecs:DescribeServices', 'iam:GetRole'):
            self.assertIn(action, self.actions)

    def test_the_reads_are_retained_with_the_role(self):
        for ref in self.resources['DeployRole']['Properties']['ManagedPolicyArns']:
            self.assertEqual(self.resources[ref['Ref']].get('DeletionPolicy'), 'Retain', ref)


class PolicySizeTest(unittest.TestCase):
    """IAM rejects a managed policy over 6,144 characters (white space not
    counted), at stack creation, so a grant added to a full policy breaks every
    new control plane. Measured with the longest names the template allows."""
    def test_every_managed_policy_fits(self):
        from ecdl_link.tests.policy_render import POLICY_SIZE_LIMIT, render, size
        resources = _template()['Resources']
        for logical, res in resources.items():
            if res['Type'] == 'AWS::IAM::ManagedPolicy':
                measured = size(render(resources, logical))
                self.assertLess(measured, POLICY_SIZE_LIMIT, logical)
    def test_the_renderer_resolves_everything(self):
        from ecdl_link.tests.policy_render import render
        resources = _template()['Resources']
        text = repr(render(resources, 'DeployLinksPolicy'))
        self.assertNotIn('${', text)
        self.assertNotIn("'Ref'", text)
        self.assertIn(':role/ecdl-*', text)
if __name__ == '__main__':
    unittest.main()
