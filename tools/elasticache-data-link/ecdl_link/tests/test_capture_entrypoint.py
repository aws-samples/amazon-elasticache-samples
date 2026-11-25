"""data-plane/debezium-server/entrypoint.sh, run for real against a stand-in
for the Debezium distribution (a run.sh that prints what it was given).

The image cannot be built here, so this is how the checks the entrypoint makes
before Debezium starts are tested: each refusal, and the JVM options it hands
to the upstream launcher.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRYPOINT = os.path.join(ROOT, 'data-plane', 'debezium-server', 'entrypoint.sh')

FAKE_RUN = """#!/bin/bash
echo "RUN.SH JAVA_OPTS=[$JAVA_OPTS]"
echo "RUN.SH ARGS=[$*]"
echo "RUN.SH PWD=[$PWD]"
echo "RUN.SH SNAPSHOT_LOCKING_MODE=[${SNAPSHOT_LOCKING_MODE-unset}]"
echo "RUN.SH DATABASE_INCLUDE_LIST=[${DATABASE_INCLUDE_LIST-unset}]"
echo "RUN.SH TLS=[${SOURCE_SSL_MODE-unset} ${SOURCE_SSLMODE-unset} ${SOURCE_DB_ENCRYPT-unset}]"
"""


class EntrypointCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='ecdl-dbz-home-')
        path = os.path.join(self.home, 'run.sh')
        with open(path, 'w') as f:
            f.write(FAKE_RUN)
        os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_entrypoint(self, *args, **env):
        base = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'ECDL_DEBEZIUM_HOME': self.home,
            'QUARKUS_PROFILE': 'mysql',
            'SIGNAL_COLLECTION': 'Inventory.ecdl_signal_x',
            'SOURCE_DB_PASSWORD': 's3cret-pw',
        }
        for key, value in env.items():
            if value is None:
                base.pop(key, None)
            else:
                base[key] = value
        return subprocess.run(['bash', ENTRYPOINT, *args], env=base,  # nosec B603 - the repo's entrypoint, test only
                              capture_output=True, text=True, timeout=30)

    def java_opts(self, proc):
        for line in proc.stdout.splitlines():
            if line.startswith('RUN.SH JAVA_OPTS=['):
                return line[len('RUN.SH JAVA_OPTS=['):-1]
        self.fail('run.sh was not reached: {} {}'.format(proc.stdout, proc.stderr))


class JvmOptions(EntrypointCase):  # Jira story F013

    def test_heap_is_three_quarters_and_oom_exits(self):
        proc = self.run_entrypoint()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.java_opts(proc),
                         '-XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError')
        self.assertIn('75%', proc.stdout)

    def test_operator_options_come_last_so_they_win(self):
        proc = self.run_entrypoint(JAVA_OPTS='-XX:MaxRAMPercentage=50 -Dx=y')
        self.assertEqual(self.java_opts(proc),
                         '-XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError '
                         '-XX:MaxRAMPercentage=50 -Dx=y')

    def test_share_can_be_changed(self):
        self.assertIn('-XX:MaxRAMPercentage=60 ',
                      self.java_opts(self.run_entrypoint(JAVA_MAX_RAM_PERCENT='60')))
        # Empty means unset, as for every other ECDL variable.
        self.assertIn('-XX:MaxRAMPercentage=75 ',
                      self.java_opts(self.run_entrypoint(JAVA_MAX_RAM_PERCENT='')))

    def test_a_bad_share_is_refused_before_the_jvm_starts(self):
        for bad in ('abc', '7', '91', '75%', '-5', '7 5'):
            proc = self.run_entrypoint(JAVA_MAX_RAM_PERCENT=bad)
            self.assertEqual(proc.returncode, 78, bad)
            self.assertIn('JAVA_MAX_RAM_PERCENT', proc.stderr, bad)
            self.assertNotIn('RUN.SH', proc.stdout, bad)
        for edge in ('10', '90'):
            self.assertEqual(self.run_entrypoint(JAVA_MAX_RAM_PERCENT=edge).returncode, 0, edge)


class JsonSecretInjectedWhole(EntrypointCase):  # Jira story F015

    def test_refused_with_the_setting_to_change_and_without_the_value(self):
        for secret in ('{"username":"admin","password":"Hunter2-x"}',
                       ' {\n  "password": "Hunter2-x",\n  "engine": "mysql"\n}\n'):
            proc = self.run_entrypoint(SOURCE_DB_PASSWORD=secret)
            self.assertEqual(proc.returncode, 78, secret)
            self.assertIn('source.secret_json_key', proc.stderr)
            self.assertIn('DBSecretJsonKey', proc.stderr)
            self.assertNotIn('Hunter2', proc.stderr + proc.stdout)
            self.assertNotIn('RUN.SH', proc.stdout)

    def test_plain_passwords_start(self):
        for secret in ('s3cret', '{braces}', 'pass"password"word', '{"user":"x"}', ''):
            proc = self.run_entrypoint(SOURCE_DB_PASSWORD=secret)
            self.assertEqual(proc.returncode, 0, '{!r}: {}'.format(secret, proc.stderr))
        self.assertEqual(self.run_entrypoint(SOURCE_DB_PASSWORD=None).returncode, 0)


class MysqlSnapshotLocking(EntrypointCase):  # Jira story F012

    def locking(self, **env):
        proc = self.run_entrypoint(**env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in proc.stdout.splitlines():
            if line.startswith('RUN.SH SNAPSHOT_LOCKING_MODE=['):
                return line[len('RUN.SH SNAPSHOT_LOCKING_MODE=['):-1]
        self.fail(proc.stdout)

    def test_no_lock_when_debezium_reads_no_data(self):
        for mode in (None, 'no_data', 'schema_only', 'never', 'recovery'):
            self.assertEqual(self.locking(SNAPSHOT_MODE=mode), 'none', mode)

    def test_the_lock_stays_when_debezium_reads_the_data(self):
        for mode in ('initial', 'when_needed', 'always', 'initial_only'):
            self.assertEqual(self.locking(SNAPSHOT_MODE=mode), 'minimal', mode)

    def test_an_explicit_choice_is_kept(self):
        self.assertEqual(self.locking(SNAPSHOT_LOCKING_MODE='extended'), 'extended')

    def test_it_is_logged(self):
        proc = self.run_entrypoint(SNAPSHOT_MODE='initial')
        self.assertIn("snapshot locking mode 'minimal'", proc.stdout)

    def test_other_engines_are_left_alone(self):
        self.assertEqual(self.locking(QUARKUS_PROFILE='postgresql'), 'unset')


class MySqlDatabases(EntrypointCase):  # review finding H3
    def databases(self, **env):
        proc = self.run_entrypoint(**env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in proc.stdout.splitlines():
            if line.startswith('RUN.SH DATABASE_INCLUDE_LIST=['):
                return line[len('RUN.SH DATABASE_INCLUDE_LIST=['):-1]
        self.fail(proc.stdout)

    def test_every_database_of_the_tables_is_captured(self):
        self.assertEqual(self.databases(SOURCE_DB_NAME='Inventory',
                                        TABLE_INCLUDE_LIST='Inventory.orders,Sales.items,Inventory.lines,Sales.x'),
                         'Inventory,Sales')

    def test_the_signalling_tables_database_is_included(self):
        self.assertEqual(self.databases(SOURCE_DB_NAME='Inventory', TABLE_INCLUDE_LIST='Inventory.orders',
                                        SIGNAL_COLLECTION='Ops.ecdl_signal_x'), 'Inventory,Ops')

    def test_one_database_stays_one(self):
        self.assertEqual(self.databases(SOURCE_DB_NAME='Inventory', TABLE_INCLUDE_LIST='Inventory.orders'),
                         'Inventory')
        self.assertEqual(self.databases(SOURCE_DB_NAME=None, TABLE_INCLUDE_LIST='Inventory.orders'),
                         'Inventory')

    def test_an_explicit_list_is_kept_and_other_engines_are_left_alone(self):
        self.assertEqual(self.databases(DATABASE_INCLUDE_LIST='A,B', TABLE_INCLUDE_LIST='C.t'), 'A,B')
        self.assertEqual(self.databases(QUARKUS_PROFILE='postgresql', TABLE_INCLUDE_LIST='public.t'), 'unset')


class SourceTls(EntrypointCase):  # review finding H1
    def tls(self, **env):
        proc = self.run_entrypoint(**env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in proc.stdout.splitlines():
            if line.startswith('RUN.SH TLS=['):
                return line[len('RUN.SH TLS=['):-1], proc.stdout
        self.fail(proc.stdout)

    def test_verified_by_default_leaves_the_engine_files_defaults(self):
        for engine in ('mysql', 'postgresql', 'sqlserver'):
            got, out = self.tls(QUARKUS_PROFILE=engine)
            self.assertEqual(got, 'unset unset unset', engine)
            self.assertIn('source TLS: verified', out, engine)

    def test_off_turns_each_engine_off(self):
        self.assertEqual(self.tls(QUARKUS_PROFILE='mysql', SOURCE_TLS='false')[0],
                         'disabled unset unset')
        self.assertEqual(self.tls(QUARKUS_PROFILE='postgresql', SOURCE_TLS='FALSE')[0],
                         'unset disable unset')
        got, out = self.tls(QUARKUS_PROFILE='sqlserver', SOURCE_TLS='false')
        self.assertEqual(got, 'unset unset false')
        self.assertIn('source TLS: OFF', out)

    def test_an_explicit_setting_wins(self):
        self.assertEqual(self.tls(QUARKUS_PROFILE='mysql', SOURCE_TLS='false',
                                  SOURCE_SSL_MODE='required')[0], 'required unset unset')

    def test_a_bad_value_is_refused(self):
        proc = self.run_entrypoint(SOURCE_TLS='maybe')
        self.assertEqual(proc.returncode, 78)
        self.assertIn('SOURCE_TLS', proc.stderr)

    def test_the_engine_files_verify(self):
        root = os.path.join(ROOT, 'data-plane', 'debezium-server')
        def text(name):
            with open(os.path.join(root, name)) as f:
                return f.read()
        self.assertIn('ssl.mode=${SOURCE_SSL_MODE:verify_identity}', text('application-mysql.properties'))
        self.assertIn('sslmode=${SOURCE_SSLMODE:verify-full}', text('application-postgresql.properties'))
        sql = text('application-sqlserver.properties')
        self.assertIn('database.encrypt=${SOURCE_DB_ENCRYPT:true}', sql)
        self.assertIn('trustServerCertificate=${SOURCE_DB_TRUST_CERT:false}', sql)
        self.assertIn('/debezium/tls/source-truststore.p12', sql)


class ExistingChecks(EntrypointCase):

    def test_profile_and_signal_collection_are_still_required(self):
        self.assertEqual(self.run_entrypoint(QUARKUS_PROFILE=None).returncode, 78)
        self.assertEqual(self.run_entrypoint(QUARKUS_PROFILE='postgres').returncode, 78)
        self.assertEqual(self.run_entrypoint(SIGNAL_COLLECTION=None).returncode, 78)

    def test_runs_the_launcher_from_the_debezium_home_with_the_arguments(self):
        proc = self.run_entrypoint('a', 'b c')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('RUN.SH ARGS=[a b c]', proc.stdout)
        self.assertIn('RUN.SH PWD=[{}]'.format(os.path.realpath(self.home)),
                      proc.stdout.replace(self.home, os.path.realpath(self.home)))


if __name__ == '__main__':
    unittest.main()
