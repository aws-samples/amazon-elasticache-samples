"""An in-memory source database and a harness that runs bulk_loader.main().

FakeSource stands in for a SourceAdapter. Tables are held by their qualified
name ("public.orders"), and a bare name resolves to DEFAULT_SCHEMA, the way
PostgreSQL's search_path or MySQL's connection database does, so a test can
tell which of two same-named tables a load read. Every table name the loader
passes in is recorded.

run_main() runs main() in this process (worker threads instead of worker
processes, so the fake source is the one the workers use) against a real
Valkey given as {'host', 'port', 'cluster'}.
"""
import concurrent.futures
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bulk_loader  # noqa: E402


class FakeSource(object):
    TABLES = {}             # qualified name -> {'pk': [...], 'columns': [...], 'rows': [...]}
    DEFAULT_SCHEMA = 'public'
    seen = []               # every table name the loader passed in
    fetches = []            # (lo, hi) of every range read
    fail = {}               # {'fetch': n} raise ConnectionError after n fetched rows;
                            # with 'lo': only in the range read that starts there;
                            # with 'times': k and 'every': n, fail k reads, each after n rows

    def __init__(self, cfg):
        self.cfg = cfg

    @classmethod
    def reset(cls, tables, default_schema='public'):
        cls.TABLES = tables
        cls.DEFAULT_SCHEMA = default_schema
        cls.seen = []
        cls.fetches = []
        cls.fail = {}

    def connect(self):
        pass

    def _table(self, table):
        type(self).seen.append(table)
        name = table if '.' in table else '{}.{}'.format(self.DEFAULT_SCHEMA, table)
        if name not in self.TABLES:
            raise RuntimeError('relation "{}" does not exist'.format(table))
        return self.TABLES[name]

    def primary_key(self, table):
        return list(self._table(table)['pk'])

    def column_types(self, table):
        return [{'name': c, 'type': t} for c, t in self._table(table)['columns']]

    def fold_identifier(self, name):
        return name

    def estimate_rows(self, table):
        return len(self._table(table)['rows'])

    def bounds(self, table, pk):
        values = [r[pk] for r in self._table(table)['rows']]
        return (min(values), max(values)) if values else (None, None)

    def _range(self, table, pk, lo, hi):
        if isinstance(lo, bulk_loader.After):
            rows = [r for r in self._table(table)['rows'] if lo.value < r[pk] <= hi]
            return sorted(rows, key=lambda r: r[pk])
        rows = [r for r in self._table(table)['rows'] if lo <= r[pk] <= hi]
        return sorted(rows, key=lambda r: r[pk])

    def key_at_offset(self, table, pk, lo, offset):
        keys = sorted(r[pk] for r in self._table(table)['rows'] if r[pk] >= lo)
        return keys[offset] if offset < len(keys) else None

    def sample(self, table, pk, lo, n):
        return [dict(r) for r in self._range(table, pk, lo, float('inf'))[:n]]

    def page(self, table, pk, lo, hi, n):
        return [dict(r) for r in self._range(table, pk, lo, hi)[:n]]

    def value_rows(self, table, pk, value):
        return [dict(r) for r in self._table(table)['rows'] if r[pk] == value]

    def fetch_iter(self, table, pk, lo, hi, batch=None):
        type(self).fetches.append((lo, hi))
        fail = type(self).fail
        armed = 'lo' not in fail or fail['lo'] == lo
        for row in self._range(table, pk, lo, hi):
            left = fail.get('fetch') if armed else None
            if left is not None:
                if left <= 0:
                    if fail.get('times', 1) > 1:
                        fail['times'] -= 1
                        fail['fetch'] = fail['every']
                    else:
                        fail.clear()
                    raise ConnectionError('connection reset by peer')
                fail['fetch'] = left - 1
            yield dict(row)


def target_from_env(testcase, env='ECDL_TEST_VALKEY', cluster=False):
    """The real Valkey a test runs against, or skip the test."""
    addr = os.environ.get(env)
    if not addr:
        testcase.skipTest(env + ' not set')
    try:
        import valkey  # noqa: F401
    except ImportError:
        testcase.skipTest('valkey-py not installed')
    host, port = addr.rsplit(':', 1)
    return {'host': host, 'port': int(port), 'ssl': False, 'cluster': cluster}


@contextlib.contextmanager
def real_valkey_modules():
    """test_target_client replaces the valkey modules with recording stubs;
    these tests need the real library, and put the stubs back afterwards."""
    saved = {k: v for k, v in sys.modules.items()
             if k == 'valkey' or k.startswith('valkey.')}
    if saved and not getattr(sys.modules.get('valkey'), '__file__', None):
        for k in saved:
            del sys.modules[k]
    try:
        yield
    finally:
        for k in [k for k in sys.modules if k == 'valkey' or k.startswith('valkey.')]:
            del sys.modules[k]
        sys.modules.update(saved)


def run_main(target, table, pk='', key_prefix=None, workers=2, extra=(), env=None,
             spec=None, pool=None, adapter=None):
    """Run bulk_loader.main() over FakeSource; returns its printed output.

    SystemExit propagates (a FATAL is a SystemExit with the message)."""
    # The fake source has no TLS; a real one is verified by default (Jira story F078).
    cfg = {'source_type': 'mysql', 'source': {'tls': False}, 'target': target,
           'table': table, 'pk': pk, 'workers': workers}
    if key_prefix is not None:
        cfg['key_prefix'] = key_prefix
    tmp = tempfile.mkdtemp(prefix='ecdl-loader-test-')
    cfg_path = os.path.join(tmp, 'loader.json')
    with open(cfg_path, 'w') as handle:
        json.dump(cfg, handle)
    argv = ['bulk_loader.py', '--config', cfg_path] + list(extra)
    if spec is not None:
        spec_path = os.path.join(tmp, 'transforms.json')
        with open(spec_path, 'w') as handle:
            json.dump(spec, handle)
        argv += ['--transforms', spec_path]
    env = dict({'LOAD_SKIP_MEMORY_CHECK': 'true'}, **(env or {}))
    saved = (bulk_loader.ADAPTERS, bulk_loader.ProcessPoolExecutor,
             bulk_loader.install_stop_handler, bulk_loader._sleep, sys.argv,
             {k: os.environ.get(k) for k in env})
    bulk_loader.ADAPTERS = {'mysql': adapter or FakeSource}
    bulk_loader.ProcessPoolExecutor = pool or concurrent.futures.ThreadPoolExecutor
    bulk_loader.install_stop_handler = lambda: None
    bulk_loader._sleep = lambda s: None
    sys.argv = argv
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            bulk_loader.main()
    finally:
        (bulk_loader.ADAPTERS, bulk_loader.ProcessPoolExecutor,
         bulk_loader.install_stop_handler, bulk_loader._sleep, sys.argv,
         old_env) = saved
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out.getvalue()


def clean(vk, *patterns):
    """Delete every key matching the patterns (scan_iter covers every node)."""
    for pattern in patterns:
        keys = list(vk.scan_iter(match=pattern, count=1000))
        for i in range(0, len(keys), 500):
            pipe = vk.pipeline(transaction=False)
            for key in keys[i:i + 500]:
                pipe.delete(key)
            pipe.execute()
