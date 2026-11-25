"""samples/: the sample links validate, name the tables the SQL creates, and
the change/compare script stays safe (fixed tables, verified TLS, no real
account in any file)."""
import os
import re
import subprocess  # nosec B404 - runs the repo's own CLI
import sys
import unittest

from ecdl_link.tests.test_control_plane_iam import ROOT

SAMPLES = os.path.join(ROOT, 'samples')
CLI = os.path.join(ROOT, 'ecdl-cli', 'ecdl')
TABLES = ['sample_customers', 'sample_products', 'sample_orders']


def read(name):
    with open(os.path.join(SAMPLES, name)) as handle:
        return handle.read()


class SampleLinkTest(unittest.TestCase):
    def test_both_links_validate(self):
        for name in ('link-mysql.yaml', 'link-postgresql.yaml'):
            # executable= runs this same interpreter; 'python3' is only its argv[0].
            run = subprocess.run(['python3', '-u', CLI, 'validate', os.path.join(SAMPLES, name)],  # nosec B603
                                 executable=sys.executable,
                                 capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(run.returncode, 0, name + run.stdout + run.stderr)

    def test_links_sql_and_script_name_the_same_tables(self):
        for link in ('link-mysql.yaml', 'link-postgresql.yaml'):
            listed = re.findall(r'^  - (\w+)$', read(link), re.M)
            self.assertEqual(listed, TABLES, link)
        for sql in ('mysql.sql', 'postgresql.sql'):
            created = re.findall(r'^CREATE TABLE (\w+)', read(sql), re.M)
            self.assertEqual(sorted(created), sorted(TABLES), sql)
            self.assertEqual(len(re.findall(r'PRIMARY KEY', read(sql))), 3, sql)
            # Never removes or rewrites anything beyond what it creates.
            code = re.sub(r'--[^\n]*', '', read(sql))
            self.assertNotRegex(code, r'(?i)\b(DROP|TRUNCATE|DELETE|ALTER|GRANT)\b')
        script = read('ecdl_sample.py')
        fixed = re.findall(r"^    \('(\w+)', '\w+'\),$", script, re.M)
        self.assertEqual(fixed, TABLES)

    def test_cleanup_drops_only_sample_tables(self):
        for sql in ('cleanup-mysql.sql', 'cleanup-postgresql.sql'):
            dropped = re.findall(r'^DROP TABLE IF EXISTS ([\w, ]+);', read(sql), re.M)
            names = {n.strip() for line in dropped for n in line.split(',')}
            self.assertEqual(names, set(TABLES) | {'ecdl_signal_ecdlsample'}, sql)


class SampleSafetyTest(unittest.TestCase):
    def test_no_real_account_or_secret(self):
        # Files only: running the suite leaves a __pycache__ directory here.
        for name in sorted(n for n in os.listdir(SAMPLES) if os.path.isfile(os.path.join(SAMPLES, n))):
            text = read(name)
            for account in re.findall(r'\b\d{12}\b', text):
                self.assertEqual(account, '111122223333', name)
            self.assertNotRegex(text, r'(?i)password\s*[:=]\s*[\'"][^\'"]+[\'"]', name)

    def test_tls_is_verified(self):
        script = read('ecdl_sample.py')
        self.assertIn("sslmode='verify-full'", script)
        self.assertIn("ssl_cert_reqs='required'", script)
        self.assertIn('ssl_check_hostname=True', script)
        self.assertIn('ctx.check_hostname = True', script)
        self.assertIn('ssl.CERT_REQUIRED', script)
        self.assertNotIn('CERT_NONE', script)
        self.assertNotIn('ssl_cert_reqs=None', script)

    def test_values_are_parameters_and_names_are_fixed(self):
        script = read('ecdl_sample.py')
        # Every statement that takes a value passes it as a parameter.
        for stmt in re.findall(r"cur\.execute\((.+?)\)\n", script, re.S):
            self.assertNotIn('%' + ' (', stmt)
            self.assertNotRegex(stmt, r"\.format\([^)]*args\.(host|database|secret)")
        # Every statement is built by sql(), which checks the names it inserts.
        self.assertEqual(len(re.findall(r"cur\.execute\(sql\(", script)), 7)
        self.assertIn("SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')", script)
        # Values are printed only on request.
        self.assertIn("if args.show_values", script)


class SampleSqlHelperTest(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('ecdl_sample', os.path.join(SAMPLES, 'ecdl_sample.py'))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def args(self, engine, schema=None):
        import argparse
        return argparse.Namespace(engine=engine, schema=schema)

    def test_names_are_quoted_per_engine(self):
        sql = self.mod.sql
        self.assertEqual(sql(self.args('mysql'), 'SELECT {pk} FROM {table}', 'sample_orders', 'order_id'),
                         'SELECT `order_id` FROM `sample_orders`')
        self.assertEqual(sql(self.args('postgresql', 'shop'), 'SELECT * FROM {table}', 'sample_orders'),
                         'SELECT * FROM "shop"."sample_orders"')
        self.assertEqual(sql(self.args('postgresql'), 'UPDATE {table} SET t = {now}', 'sample_orders'),
                         'UPDATE "public"."sample_orders" SET t = CURRENT_TIMESTAMP')

    def test_unsafe_names_are_refused(self):
        for schema in ('x"; DROP TABLE t; --', 'a b', '1abc', 'a' * 64, ''):
            if not schema:
                continue
            with self.assertRaises(SystemExit):
                self.mod.sql(self.args('postgresql', schema), 'SELECT * FROM {table}', 'sample_orders')
        with self.assertRaises(SystemExit):
            self.mod.sql(self.args('mysql'), 'SELECT * FROM {table}', 'sample`orders')


if __name__ == '__main__':
    unittest.main()
