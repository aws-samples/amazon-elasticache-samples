"""YAML reading through yaml.safe_load only.

safe_load builds plain Python values (dict, list, str, numbers, dates) and never
calls a constructor that can run code. Two things on top of it:

- CloudFormation's short-form intrinsic tags (!Ref, !Sub, ...) are registered on
  SafeLoader, each building ``{'<Tag>': value}`` (``!Ref X`` -> ``{'Ref': 'X'}``),
  the same shape as the long form ``Fn::...``/``Ref``. Without them safe_load
  refuses every template. The constructors build only plain values.
- ``check_plain`` refuses aliases (the "billion laughs" document: a few hundred
  bytes of nested aliases expand to millions of nodes) and custom ``!`` tags, for
  text a user sends. It only reads parser events; nothing is constructed.
"""
import yaml

CFN_TAGS = ('Ref', 'Sub', 'GetAtt', 'Select', 'Join', 'Split', 'If', 'Equals', 'And',
            'Or', 'Not', 'FindInMap', 'Base64', 'Cidr', 'GetAZs', 'ImportValue',
            'Transform', 'Condition')


def _cfn_constructor(tag):
    def construct(loader, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {tag: value}
    return construct


for _tag in CFN_TAGS:
    yaml.SafeLoader.add_constructor('!' + _tag, _cfn_constructor(_tag))


def load_cfn(stream):
    """A CloudFormation template (text or open file) as plain values."""
    return yaml.safe_load(stream)


def check_plain(text, what='a link config', allow_aliases=False):
    """Raise yaml.YAMLError on an alias or a custom ``!`` tag, with its position.

    Parse errors are raised here too, at the same place safe_load would raise them.
    The CLI reads files with allow_aliases=True, as it always has; the tags are
    refused there too, since SafeLoader now knows the CloudFormation ones.
    """
    scanner = yaml.SafeLoader(text)
    try:
        while scanner.check_event():
            event = scanner.get_event()
            if isinstance(event, yaml.AliasEvent) and not allow_aliases:
                raise yaml.composer.ComposerError(
                    None, None, 'aliases (*{}) are not accepted in {}'.format(event.anchor, what),
                    event.start_mark)
            tag = getattr(event, 'tag', None)
            if isinstance(event, yaml.NodeEvent) and tag and tag.startswith('!') and not tag.startswith('!!'):
                raise yaml.constructor.ConstructorError(
                    None, None, 'tags ({}) are not accepted in {}'.format(tag, what), event.start_mark)
    finally:
        scanner.dispose()
