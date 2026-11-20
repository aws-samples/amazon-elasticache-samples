"""Render a control-plane IAM policy document the way IAM will see it.

IAM limits a customer managed policy to 6,144 characters, not counting white
space. The documents in ecdl-control-plane.yaml are full of !Sub, !If and
!Join, so their size depends on the stack name, the region and the partition.
render() resolves those with the longest values the template allows, and takes
the longer branch of every !If, so the size it reports is an upper bound.
"""
import json
import re

POLICY_SIZE_LIMIT = 6144

# Longest values each pseudo parameter can take.
WORST_CASE = {
    'AWS::Partition': 'aws-us-gov',
    'AWS::Region': 'ap-southeast-5',
    'AWS::AccountId': '123456789012',
    # The role names (64-character limit) leave room for 32.
    'AWS::StackName': 'x' * 32,
    'AWS::URLSuffix': 'amazonaws.com.cn',
    'ManagedResourcePrefix': 'ecdl',
    'TemplateBucket': 'b' * 63,
    'DeployVpcId': 'vpc-0123456789abcdef0',
    'PipelineSecretArns': [],
    'MonitorSourceSecretArns': [],
}


def secret_arn(n, values=WORST_CASE):
    """A Secrets Manager ARN of a typical length (a 20-character name)."""
    return 'arn:{}:secretsmanager:{}:{}:secret:{}-AbCdEf'.format(
        values['AWS::Partition'], values['AWS::Region'], values['AWS::AccountId'],
        'customer-db-secret{:02d}'.format(n))


class _Renderer(object):
    def __init__(self, resources, values, conditions=None):
        self.resources = resources
        # Condition name -> True/False. An !If on a condition not given here
        # takes the longer branch.
        self.conditions = dict(conditions or {})
        self.values = dict(values)
        v = self.values
        self.values.setdefault('AWS::StackId', 'arn:{}:cloudformation:{}:{}:stack/{}/{}'.format(
            v['AWS::Partition'], v['AWS::Region'], v['AWS::AccountId'], v['AWS::StackName'],
            '0' * 36))

    def _name(self, logical):
        """The physical name of a resource of this template (for Ref and GetAtt)."""
        res = self.resources[logical]
        props = res.get('Properties') or {}
        v = self.values
        if res['Type'] == 'AWS::IAM::ManagedPolicy':
            return 'arn:{}:iam::{}:policy/{}'.format(
                v['AWS::Partition'], v['AWS::AccountId'], self.render(props['ManagedPolicyName']))
        if res['Type'] == 'AWS::IAM::Role':
            return 'arn:{}:iam::{}:role/{}'.format(
                v['AWS::Partition'], v['AWS::AccountId'], self.render(props['RoleName']))
        if res['Type'] == 'AWS::Logs::LogGroup':
            # Ref of a log group is its name.
            return self.render(props['LogGroupName'])
        raise KeyError('no physical name for {} ({})'.format(logical, res['Type']))

    def _ref(self, name):
        if name in self.values:
            return self.values[name]
        return self._name(name)

    def _sub(self, arg):
        text, local = (arg, {}) if isinstance(arg, str) else (arg[0], arg[1])

        def one(match):
            key = match.group(1)
            if key in local:
                return str(self.render(local[key]))
            if key.endswith('.Arn'):
                return self._name(key[:-4])
            value = self._ref(key)
            return ','.join(value) if isinstance(value, list) else str(value)
        return re.sub(r'\$\{([^}!]+)\}', one, text)

    def render(self, node):
        if isinstance(node, list):
            out = []
            for item in node:
                value = self.render(item)
                if value is _NO_VALUE:
                    continue
                out.append(value)
            return out
        if not isinstance(node, dict):
            return node
        if len(node) == 1:
            (key, arg), = node.items()
            if key == 'Ref':
                return _NO_VALUE if arg == 'AWS::NoValue' else self._ref(arg)
            if key == 'Sub':
                return self._sub(arg)
            if key == 'GetAtt':
                logical, attr = arg.split('.', 1) if isinstance(arg, str) else arg
                if attr != 'Arn':
                    raise ValueError('only GetAtt .Arn is rendered: {}'.format(arg))
                return self._name(logical)
            if key == 'If':
                if arg[0] in self.conditions:
                    return self.render(arg[1] if self.conditions[arg[0]] else arg[2])
                branches = [self.render(arg[1]), self.render(arg[2])]
                sizes = [len(json.dumps(b)) if b is not _NO_VALUE else 0 for b in branches]
                return branches[0] if sizes[0] >= sizes[1] else branches[1]
            if key == 'Join':
                sep, items = arg
                items = self.render(items)
                if not isinstance(items, list):
                    items = [items]
                return sep.join(','.join(i) if isinstance(i, list) else str(i) for i in items)
            if key == 'Split':
                sep, text = arg
                return [part for part in str(self.render(text)).split(sep)]
        return {k: self.render(v) for k, v in node.items()
                if self.render(v) is not _NO_VALUE}


class _NoValue(object):
    pass


_NO_VALUE = _NoValue()


def render(resources, logical, values=None, conditions=None):
    """The policy document of a ManagedPolicy resource, resolved."""
    doc = resources[logical]['Properties']['PolicyDocument']
    return _Renderer(resources, values or WORST_CASE, conditions).render(doc)


def size(document):
    """IAM's count: the JSON without white space between tokens. (Spaces
    inside strings are counted, so this is never an undercount.)"""
    return len(json.dumps(document, separators=(',', ':')))
