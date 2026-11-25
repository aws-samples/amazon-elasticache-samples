"""YAML is read with yaml.safe_load only (ACAT "Unsafe YAML Load")."""
import os
import unittest

import yaml

from ecdl_link.link import LinkValidationError, loads as parse_text
from ecdl_link.safe_yaml import check_plain, load_cfn

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP_DIRS = {'.git', 'node_modules', 'build', 'dist', '__pycache__', '.venv', 'venv'}


def _files(top, suffixes):
    """Files under ROOT/top ending in one of suffixes, as paths relative to ROOT.
    A walk, not git ls-files: the published copy is not a repository of its own."""
    out = []
    for folder, dirs, names in os.walk(os.path.join(ROOT, top)):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        out += [os.path.relpath(os.path.join(folder, n), ROOT) for n in sorted(names) if n.endswith(suffixes)]
    return out


class NoUnsafeLoadTest(unittest.TestCase):
    def test_no_yaml_load_call_with_a_loader_anywhere(self):
        """Every reader goes through yaml.safe_load: no Python file in the repo calls the
        loaders that take a Loader argument, or the unsafe and full variants."""
        files = _files('.', ('.py',))
        pattern = ('yaml.' + 'load(', 'yaml.' + 'load_all(', 'yaml.' + 'unsafe_load', 'yaml.' + 'full_load',
                   'Loader' + '=', 'yaml.' + 'UnsafeLoader', 'yaml.' + 'FullLoader')
        hits = []
        for name in files:
            with open(os.path.join(ROOT, name), encoding='utf-8') as handle:
                for number, line in enumerate(handle, 1):
                    if any(p in line for p in pattern):
                        hits.append('{}:{}: {}'.format(name, number, line.strip()))
        self.assertEqual(hits, [])

    def test_python_object_tags_are_refused(self):
        for text in ('!!python/object/apply:os.system ["true"]\n',
                     'a: !!python/name:os.system\n'):
            with self.assertRaises(yaml.constructor.ConstructorError):
                load_cfn(text)
            with self.assertRaises(yaml.YAMLError):
                check_plain(text)
                yaml.safe_load(text)


class CloudFormationTest(unittest.TestCase):
    def test_short_form_tags_read_as_long_form_values(self):
        doc = load_cfn('A: !Ref X\nB: !Sub "${X}-y"\nC: !GetAtt R.Arn\n'
                       'D: !If [Cond, !Ref X, !Ref "AWS::NoValue"]\nE: !Join ["", [a, b]]\n')
        self.assertEqual(doc, {'A': {'Ref': 'X'}, 'B': {'Sub': '${X}-y'}, 'C': {'GetAtt': 'R.Arn'},
                               'D': {'If': ['Cond', {'Ref': 'X'}, {'Ref': 'AWS::NoValue'}]},
                               'E': {'Join': ['', ['a', 'b']]}})

    def test_every_template_in_the_repo_loads(self):
        files = _files('infra', ('.yaml', '.yml'))
        self.assertTrue(files)
        for name in files:
            with open(os.path.join(ROOT, name), encoding='utf-8') as handle:
                doc = load_cfn(handle)
            self.assertIn('Resources', doc, name)


class UserTextTest(unittest.TestCase):
    def test_aliases_are_refused_with_their_position(self):
        with self.assertRaises(yaml.YAMLError) as ctx:
            check_plain('a: &a [x, x]\nb: &b [*a, *a]\n')
        self.assertIn('aliases', str(ctx.exception))
        self.assertEqual(ctx.exception.problem_mark.line + 1, 2)

    def test_tags_are_refused_even_the_cloudformation_ones(self):
        for text in ('name: !Ref x\n', 'name: !custom x\n'):
            with self.assertRaises(yaml.YAMLError):
                check_plain(text)

    def test_standard_tags_and_plain_documents_pass(self):
        check_plain('name: !!str 12\nlist: [1, 2]\nanchor: &n x\n')

    def test_the_cli_refuses_a_tag_and_keeps_accepting_aliases(self):
        with self.assertRaises(LinkValidationError):
            parse_text('name: !Ref x\n', 'link.yaml')
        doc = parse_text('base: &b x\nname: *b\n', 'link.yaml')
        self.assertEqual(doc['name'], 'x')


if __name__ == '__main__':
    unittest.main()
