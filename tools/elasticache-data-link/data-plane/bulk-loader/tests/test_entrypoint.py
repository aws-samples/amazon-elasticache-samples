"""entrypoint.sh: the environment ECS sets becomes the right loader command.

The script is run for real, with `python3` on PATH replaced by a wrapper that
prints the arguments it would have started bulk_loader.py with (and runs the
real interpreter for the script's own small helpers).
"""
import json
import os
import shutil
import stat
import subprocess  # nosec B404 - runs the repo's own entrypoint script in a test
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(os.path.dirname(HERE), 'entrypoint.sh')

WRAPPER = """#!/bin/sh
if [ "$1" = /app/bulk_loader.py ]; then
  shift
  exec "{python}" -c 'import json, sys; print("ARGV=" + json.dumps(sys.argv[1:]))' "$@"
fi
exec "{python}" "$@"
"""

BASE_ENV = {'SOURCE_ENGINE': 'postgresql', 'SOURCE_DB_HOST': 'db', 'VALKEY_HOST': 'cache'}


class EntrypointHarness(object):
    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix='ecdl-entrypoint-')
        path = os.path.join(self.bin, 'python3')
        with open(path, 'w') as handle:
            handle.write(WRAPPER.format(python=sys.executable))
        os.chmod(path, stat.S_IRWXU)

    def tearDown(self):
        shutil.rmtree(self.bin, ignore_errors=True)

    def run_entrypoint(self, **env):
        full = {'PATH': self.bin + os.pathsep + '/usr/bin:/bin'}
        full.update(BASE_ENV)
        full.update(env)
        # Run where there are files, so an unquoted glob would expand.
        return subprocess.run(['/bin/sh', ENTRYPOINT], env=full, capture_output=True,  # nosec B603
                              text=True, timeout=60, cwd=self.bin)

    def raw_argv(self, **env):
        proc = self.run_entrypoint(**env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [l for l in proc.stdout.splitlines() if l.startswith('ARGV=')][-1]
        return json.loads(line[len('ARGV='):])

    def argv(self, **env):
        out = {}
        for a in self.raw_argv(**env):
            name, eq, value = a.partition('=')
            self.assertTrue(name.startswith('--') and eq, 'every argument is --option=value: %r' % a)
            out[name] = value
        return out


class EntrypointTest(EntrypointHarness, unittest.TestCase):
    def test_the_qualified_name_is_queried_and_the_bare_name_keys(self):
        args = self.argv(TABLE='customers', TABLES='orders,customers',
                         SOURCE_TABLES='sales.orders,sales.customers',
                         LINK_KEY_PREFIX='v2')
        self.assertEqual(args['--table'], 'sales.customers')
        self.assertEqual(args['--key-prefix'], 'v2:customers:')

    def test_mysql_database_qualified(self):
        args = self.argv(SOURCE_ENGINE='mysql', TABLE='orders', TABLES='orders',
                         SOURCE_TABLES='Inventory.orders')
        self.assertEqual(args['--table'], 'Inventory.orders')
        self.assertNotIn('--key-prefix', args, 'the loader defaults to <bare table>:')

    def test_a_single_table_needs_no_override(self):
        args = self.argv(TABLES='orders', SOURCE_TABLES='sales.orders')
        self.assertEqual(args['--table'], 'sales.orders')

    def test_a_stack_without_source_tables_reads_the_bare_name(self):
        # A stack deployed before SourceTables existed.
        args = self.argv(TABLE='orders', TABLES='orders,customers', SOURCE_TABLES='')
        self.assertEqual(args['--table'], 'orders')

    def test_a_qualified_table_given_by_hand_is_used_as_given(self):
        args = self.argv(TABLE='sales.orders', TABLES='orders',
                         SOURCE_TABLES='public.orders', LINK_KEY_PREFIX='v2:')
        self.assertEqual(args['--table'], 'sales.orders')
        self.assertEqual(args['--key-prefix'], 'v2:orders:')

    def test_primary_keys_still_match_by_position(self):
        args = self.argv(TABLE='customers', TABLES='orders,customers',
                         SOURCE_TABLES='sales.orders,sales.customers',
                         PRIMARY_KEYS='order_id,customer_id')
        self.assertEqual(args['--pk'], 'customer_id')

    def test_lists_out_of_step_are_refused(self):
        for source_tables in ('sales.orders', 'sales.customers,sales.orders',
                              'sales.orders,sales.customers,sales.items'):
            proc = self.run_entrypoint(TABLE='customers', TABLES='orders,customers',
                                       SOURCE_TABLES=source_tables)
            self.assertEqual(proc.returncode, 78, (source_tables, proc.stdout))
            self.assertIn('SOURCE_TABLES', proc.stderr)
            self.assertNotIn('ARGV=', proc.stdout)


class EntrypointQuotingTest(EntrypointHarness, unittest.TestCase):
    """Jira story F083: configuration values reach the loader as one argument each."""

    def test_a_key_prefix_with_spaces_and_globs_is_one_argument(self):
        for prefix in ('a b:', '*:', 'x --progress-key evil', '--fresh'):
            args = self.raw_argv(TABLE='orders', TABLES='orders', KEY_PREFIX=prefix)
            self.assertIn('--key-prefix=' + prefix, args)
            self.assertNotIn('--fresh', args)
            self.assertNotIn('--progress-key', args)
            self.assertEqual(len(args), 5, args)

    def test_a_link_key_prefix_with_a_space_is_one_argument(self):
        args = self.raw_argv(TABLE='orders', TABLES='orders', LINK_KEY_PREFIX='v 2')
        self.assertIn('--key-prefix=v 2:orders:', args)

    def test_a_glob_does_not_expand_to_file_names(self):
        # Run in a directory with files, so an unquoted * would expand.
        args = self.raw_argv(TABLE='orders', TABLES='orders', KEY_PREFIX='*')
        self.assertIn('--key-prefix=*', args)

    def test_bounds_are_passed_when_whole_numbers(self):
        args = self.argv(TABLE='orders', TABLES='orders', MIN_PK='-5', MAX_PK='900')
        self.assertEqual(args['--min-pk'], '-5')
        self.assertEqual(args['--max-pk'], '900')

    def test_bounds_that_are_not_whole_numbers_are_refused(self):
        for bad in ('1 --fresh', '1.5', 'abc', '-', '5-', '*'):
            for name in ('MIN_PK', 'MAX_PK'):
                proc = self.run_entrypoint(TABLE='orders', TABLES='orders', **{name: bad})
                self.assertEqual(proc.returncode, 78, (name, bad, proc.stdout))
                self.assertIn(name, proc.stderr)
                self.assertNotIn('ARGV=', proc.stdout)

    def test_transforms_are_one_argument(self):
        args = self.argv(TABLE='orders', TABLES='orders', TRANSFORMS_JSON='{"tables": {}}')
        # The file the entrypoint writes in the task's own /tmp.
        self.assertEqual(args['--transforms'], '/tmp/transforms.json')  # nosec B108


class TemplateWiringTest(unittest.TestCase):
    """The loader task definition sets what the entrypoint reads."""
    def environment(self):
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
                            'infra', 'ecdl-loader-ecs.yaml')
        with open(path) as handle:
            text = handle.read()
        return dict(re.findall(r'- \{ Name: (\w+), Value: !Ref (\w+) \}', text))

    def test_the_table_lists_reach_the_task(self):
        env = self.environment()
        self.assertEqual(env.get('TABLES'), 'Tables')
        self.assertEqual(env.get('SOURCE_TABLES'), 'SourceTables')
        self.assertEqual(env.get('PRIMARY_KEYS'), 'PrimaryKeys')
        self.assertEqual(env.get('LINK_KEY_PREFIX'), 'KeyPrefix')

    def test_the_progress_key_base_reaches_the_task(self):
        self.assertEqual(self.environment().get('PROGRESS_KEY_BASE'), 'ProgressKeyBase')


if __name__ == '__main__':
    unittest.main()
