"""Jira story F077: the loader's transform engine against the cases the Go sink also runs.

tools/go-sink/testdata/transform_cases.json holds the number, expression,
filter and rename rules; the Go tests (transform_cases_test.go) and the link
checker's tests read the same file, so the engines cannot drift apart again.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import transforms  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CASES = os.path.join(REPO, 'tools', 'go-sink', 'testdata', 'transform_cases.json')


def read_cases():
    with open(CASES, encoding='utf-8') as handle:
        cases = json.load(handle)
    assert len(cases['expressions']) >= 50 and len(cases['invalid_expressions']) >= 20
    return cases


def computed(expr):
    return {'tables': {'t': {'computed': [{'name': 'out', 'expr': expr}]}}}


class TransformCasesTest(unittest.TestCase):
    def test_expressions(self):
        for case in read_cases()['expressions']:
            with self.subTest(expr=case['expr'], row=case['row']):
                spec = transforms.load_spec(json.dumps(computed(case['expr'])))
                out, keep = transforms.apply(spec, 't', case['row'])
                self.assertTrue(keep)
                self.assertEqual(out['out'], case['want'])

    def test_invalid_expressions_are_refused_at_load(self):
        for expr in read_cases()['invalid_expressions']:
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError) as caught:
                    transforms.check_expr(expr)
                with self.assertRaises(ValueError) as caught:
                    transforms.load_spec(json.dumps(computed(expr)))
                self.assertIn('computed[0] (out)', str(caught.exception))

    def test_filters(self):
        for case in read_cases()['filters']:
            with self.subTest(filter=case['filter'], row=case['row']):
                spec = {'tables': {'t': {'filter': case['filter']}}}
                _out, keep = transforms.apply(spec, 't', case['row'])
                self.assertEqual(keep, case['keep'])

    def test_apply(self):
        for case in read_cases()['apply']:
            with self.subTest(case['name']):
                spec = transforms.load_spec(json.dumps({'tables': {'t': case['spec']}}))
                out, keep = transforms.apply(spec, 't', dict(case['row']))
                self.assertEqual(keep, case['keep'])
                if keep:
                    self.assertEqual(out, case['fields'])
                # Column order must not matter (it does not in Go).
                out2, _ = transforms.apply(spec, 't', dict(reversed(list(case['row'].items()))))
                self.assertEqual(out2, out)

    def test_invalid_specs(self):
        for case in read_cases()['invalid_specs']:
            with self.subTest(case['name']):
                with self.assertRaises(ValueError):
                    transforms.load_spec(json.dumps(case['spec']))

    def test_a_spec_file_is_checked_too(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
            json.dump(computed('a %% 2'), handle)
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(ValueError):
            transforms.load_spec(handle.name)

    def test_the_shipped_spec_loads(self):
        transforms.load_spec(os.path.join(REPO, 'data-plane', 'transforms.json'))


if __name__ == '__main__':
    unittest.main()
