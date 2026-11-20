"""Jira story F077: link validation refuses the expressions the sink and the loader refuse.

The cases are tools/go-sink/testdata/transform_cases.json, which the Go sink
and the bulk loader tests also read.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ecdl_link import problems_for  # noqa: E402
from ecdl_link.exprcheck import expr_problem  # noqa: E402
from ecdl_link.tests.test_link import minimal  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CASES = os.path.join(REPO, 'tools', 'go-sink', 'testdata', 'transform_cases.json')


def read_cases():
    with open(CASES, encoding='utf-8') as handle:
        return json.load(handle)


class SharedCasesTest(unittest.TestCase):
    def test_valid_expressions_pass(self):
        for case in read_cases()['expressions']:
            self.assertIsNone(expr_problem(case['expr']), case['expr'])

    def test_invalid_expressions_are_reported(self):
        cases = read_cases()['invalid_expressions']
        self.assertGreaterEqual(len(cases), 20)
        for expr in cases:
            self.assertIsNotNone(expr_problem(expr), expr)

    def test_not_text(self):
        self.assertEqual(expr_problem(5), 'must be text')


def with_transform(spec):
    doc = minimal()
    table = doc['tables'][0]
    doc['transforms'] = {'tables': {table: spec}}
    return doc, table


class LinkValidationTest(unittest.TestCase):
    def paths(self, doc):
        return {p.path: p.message for p in problems_for(doc)}

    def test_an_invalid_expression_is_a_problem(self):
        doc, table = with_transform({'computed': [
            {'name': 'ok', 'expr': 'price * 1e2'}, {'name': 'bad', 'expr': 'price % 2'}]})
        found = self.paths(doc)
        path = 'transforms.tables.{}.computed[1].expr'.format(table)
        self.assertIn(path, found)
        self.assertIn("position 7 ('%')", found[path])
        self.assertNotIn('transforms.tables.{}.computed[0].expr'.format(table), found)

    def test_two_columns_renamed_to_one_name_is_a_problem(self):
        doc, table = with_transform({'rename': {'a': 'x', 'b': 'x', 'c': 'y'}})
        found = self.paths(doc)
        msg = found.get('transforms.tables.{}.rename'.format(table), '')
        self.assertIn("'a' and 'b' all to 'x'", msg)
        doc, table = with_transform({'rename': {'a': 'b', 'b': 'a'}})
        self.assertNotIn('transforms.tables.{}.rename'.format(table), self.paths(doc))


if __name__ == '__main__':
    unittest.main()
