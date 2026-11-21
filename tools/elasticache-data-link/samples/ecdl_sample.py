#!/usr/bin/env python3
"""Try ECDL on the sample tables: make changes at the source, then check that
the cache holds exactly what the source holds.

  python3 samples/ecdl_sample.py changes --engine mysql --host <writer> \
      --database <db> --secret-arn <arn>
  python3 samples/ecdl_sample.py compare --engine mysql --host <writer> \
      --database <db> --secret-arn <arn> --cache-host <cache endpoint>

Run it from a host that can reach both the database and the cache (the same
VPC), with AWS credentials that can read the secret(s).

Safety:
- It touches only the three sample tables (sample_customers, sample_products,
  sample_orders). Table names are fixed in this file, never taken from input.
- Passwords come from Secrets Manager and are never printed or logged.
- Connections are TLS with the server certificate and host name verified:
  the database against ecdl_link/rds-ca-bundle.pem (Amazon RDS's CAs; run
  `make ca-bundle` once to download it), the
  cache against the system CAs (ElastiCache certificates are Amazon-issued).
- `compare` only reads. It prints row ids and column names, not values,
  unless --show-values is given.

Needs: boto3, valkey, and pymysql (MySQL) or psycopg2-binary (PostgreSQL).
"""
import argparse
import collections
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The cache stores each value the way Debezium sends it (dates as epoch
# numbers, decimals as text, ...). The loader's own encoder does the same
# conversion, so the comparison uses it rather than a second copy of the rules.
sys.path.insert(0, os.path.join(ROOT, 'data-plane', 'bulk-loader'))
import cdc_values  # noqa: E402

# Table -> primary-key column. The only tables this script reads or changes.
TABLES = collections.OrderedDict([
    ('sample_customers', 'customer_id'),
    ('sample_products', 'product_id'),
    ('sample_orders', 'order_id'),
])
RDS_CA = os.path.join(ROOT, 'ecdl_link', 'rds-ca-bundle.pem')


def secret_credential(secret_arn, region):
    """(username, password) from a secret: JSON with username/password, or the
    password alone as plain text."""
    import boto3
    raw = boto3.client('secretsmanager', region_name=region).get_secret_value(
        SecretId=secret_arn)['SecretString']
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, raw
    if isinstance(doc, dict):
        return doc.get('username'), doc.get('password')
    return None, raw


def connect_db(args):
    user, password = secret_credential(args.secret_arn, args.region)
    user = args.username or user
    if not user:
        sys.exit('the secret has no username; pass --username')
    if args.engine == 'mysql':
        import pymysql
        import ssl
        # The same check the loader makes: certificate and host name, TLS 1.2+.
        ctx = ssl.create_default_context(cafile=args.ca)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return pymysql.connect(
            host=args.host, port=args.port or 3306, user=user, password=password,
            database=args.database, charset='utf8mb4', autocommit=True,
            init_command="SET time_zone = '+00:00'", ssl=ctx)
    import psycopg2
    conn = psycopg2.connect(
        host=args.host, port=args.port or 5432, user=user, password=password,
        dbname=args.database, sslmode='verify-full', sslrootcert=args.ca,
        options='-c TimeZone=UTC')
    conn.autocommit = True
    return conn


def connect_cache(args):
    kwargs = dict(host=args.cache_host, port=args.cache_port, ssl=True,
                  ssl_cert_reqs='required', ssl_check_hostname=True,
                  decode_responses=True)
    if args.cache_secret_arn:
        user, password = secret_credential(args.cache_secret_arn, args.region)
        kwargs['password'] = password
        if user:
            kwargs['username'] = user
    import valkey
    from valkey.cluster import ValkeyCluster
    try:
        client = ValkeyCluster(**kwargs)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 - a cache without cluster mode refuses CLUSTER SLOTS
        if 'cluster' not in str(exc).lower():
            raise
    client = valkey.Valkey(**kwargs)
    client.ping()
    return client


def schema_of(conn, args):
    return args.database if args.engine == 'mysql' else (args.schema or 'public')


def columns(conn, args, table):
    """The column descriptions cdc_values needs, from the catalogue."""
    cur = conn.cursor()
    if args.engine == 'mysql':
        cur.execute(
            'SELECT COLUMN_NAME, DATA_TYPE, DATETIME_PRECISION, NUMERIC_SCALE, '
            'COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', (schema_of(conn, args), table))
        return [{'name': r[0], 'type': r[1], 'precision': r[2], 'scale': r[3],
                 'column_type': r[4], 'nullable': r[5]} for r in cur.fetchall()]
    cur.execute(
        'SELECT column_name, data_type, datetime_precision, numeric_scale, '
        'udt_name, is_nullable FROM information_schema.columns '
        'WHERE table_schema = %s AND table_name = %s', (schema_of(conn, args), table))
    return [{'name': r[0], 'type': r[1], 'precision': r[2], 'scale': r[3],
             'column_type': r[4], 'nullable': r[5]} for r in cur.fetchall()]


# Names that may appear in SQL: plain identifiers only, so quoting keeps them
# literal. Table and key names come from TABLES; --schema is checked too.
SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')


def sql(args, template, table, pk=None):
    """`template` with {table}, {pk} and {now} filled in. Values are never
    put in the text: they are passed to execute() as parameters."""
    names = [table] + ([pk] if pk else []) + ([args.schema] if args.schema else [])
    for name in names:
        if not SAFE_NAME.match(name):
            sys.exit('refusing the name {!r}: letters, digits and _ only'.format(name))
    q = '`{}`' if args.engine == 'mysql' else '"{}"'
    tbl = q.format(table) if args.engine == 'mysql' else '{}.{}'.format(
        q.format(args.schema or 'public'), q.format(table))
    now = 'CURRENT_TIMESTAMP(3)' if args.engine == 'mysql' else 'CURRENT_TIMESTAMP'
    return template.format(table=tbl, pk=q.format(pk) if pk else '', now=now)  # nosec B608 - names checked and quoted above


def cmd_changes(args):
    conn = connect_db(args)
    cur = conn.cursor()
    rnd = random.Random(args.seed)  # nosec B311 - picks sample rows, not a secret
    counts = collections.Counter()

    def ids(table, pk):
        cur.execute(sql(args, 'SELECT {pk} FROM {table}', table, pk))
        return [r[0] for r in cur.fetchall()]

    def top(table, pk):
        cur.execute(sql(args, 'SELECT COALESCE(MAX({pk}), 0) FROM {table}', table, pk))
        return cur.fetchone()[0]

    products, customers = ids('sample_products', 'product_id'), ids('sample_customers', 'customer_id')
    # Updates: prices, the status a filter would use, stock flags, notes.
    for pid in rnd.sample(products, min(args.updates, len(products))):
        cur.execute(sql(args, 'UPDATE {table} SET price = price + 1, status = %s, in_stock = %s, '
                        'updated_at = {now} WHERE product_id = %s', 'sample_products'),
                    (rnd.choice(['active', 'retired']), rnd.random() < 0.5, pid))
        counts['updated sample_products'] += 1
    for cid in rnd.sample(customers, min(args.updates // 5 or 1, len(customers))):
        cur.execute(sql(args, 'UPDATE {table} SET tier = %s WHERE customer_id = %s', 'sample_customers'),
                    (rnd.choice(['gold', 'silver', 'bronze']), cid))
        counts['updated sample_customers'] += 1
    # Inserts: new orders for existing customers and products.
    next_id = top('sample_orders', 'order_id')
    for i in range(args.inserts):
        next_id += 1
        cur.execute(sql(args, 'INSERT INTO {table} (order_id, customer_id, product_id, quantity, total, '
                        'note, ordered_at) VALUES (%s, %s, %s, %s, %s, %s, {now})', 'sample_orders'),
                    (next_id, rnd.choice(customers), rnd.choice(products), 1 + i % 5,
                     round(rnd.uniform(1, 500), 2), None if i % 7 == 0 else 'new order'))
        counts['inserted sample_orders'] += 1
    # Deletes: the oldest orders.
    orders = sorted(ids('sample_orders', 'order_id'))[:args.deletes]
    for oid in orders:
        cur.execute(sql(args, 'DELETE FROM {table} WHERE order_id = %s', 'sample_orders'), (oid,))
        counts['deleted sample_orders'] += 1
    for k in sorted(counts):
        print('{:>6}  {}'.format(counts[k], k))
    print('done. Run `compare` to check the cache (allow a few seconds for CDC).')


def compare_once(args, conn, cache):
    problems = 0
    for table, pk in TABLES.items():
        enc = cdc_values.row_encoder(args.engine, columns(conn, args, table))
        cur = conn.cursor()
        cur.execute(sql(args, 'SELECT * FROM {table}', table))
        names = [d[0] for d in cur.description]
        rows = {}
        for r in cur.fetchall():
            row = dict(zip(names, r))
            rows[str(row[pk])] = enc(row)
        prefix = args.key_prefix + table + ':'
        keys = {k[len(prefix):] for k in cache.scan_iter(match=prefix + '*', count=1000)}
        missing, extra = sorted(set(rows) - keys), sorted(keys - set(rows))
        bad, example = collections.Counter(), {}
        ids = sorted(set(rows) & keys)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            pipe = cache.pipeline(transaction=False)
            for rid in chunk:
                pipe.hgetall(prefix + rid)
            for rid, cached in zip(chunk, pipe.execute()):
                for col, want in rows[rid].items():
                    got = cached.get(col, '')
                    if got != want:
                        bad[col] += 1
                        example.setdefault(col, (rid, want, got))
        mism = sum(bad.values())
        problems += len(missing) + len(extra) + mism
        print('{:<17} rows {:>6}  cached {:>6}  missing {}  extra {}  field mismatches {}'.format(
            table, len(rows), len(keys), len(missing), len(extra), mism))
        if missing:
            print('    missing ids (first 5): {}'.format(', '.join(missing[:5])))
        if extra:
            print('    extra ids, deleted at the source (first 5): {}'.format(', '.join(extra[:5])))
        for col, n in bad.most_common(10):
            rid, want, got = example[col]
            detail = ' source {!r} cache {!r}'.format(want, got) if args.show_values else ''
            print('    {}: {} row(s), e.g. id {}{}'.format(col, n, rid, detail))
    return problems


def cmd_compare(args):
    conn, cache = connect_db(args), connect_cache(args)
    deadline = time.time() + args.wait
    while True:
        problems = compare_once(args, conn, cache)
        if problems == 0:
            print('OK: the cache matches the source.')
            return 0
        if time.time() >= deadline:
            print('NOT OK: {} difference(s). If changes were just made, wait and run again; '
                  'otherwise see docs/operations.md.'.format(problems))
            return 1
        print('... {} difference(s), checking again in 5 s'.format(problems))
        time.sleep(5)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    for name, helptext in (('changes', 'insert, update and delete rows in the sample tables'),
                           ('compare', 'check every sample row against the cache (read only)')):
        s = sub.add_parser(name, help=helptext)
        s.add_argument('--engine', choices=['mysql', 'postgresql'], required=True)
        s.add_argument('--host', required=True, help='the database writer endpoint')
        s.add_argument('--port', type=int)
        s.add_argument('--database', required=True)
        s.add_argument('--schema', help='PostgreSQL schema (default public)')
        s.add_argument('--secret-arn', required=True, help='the database secret')
        s.add_argument('--username', help='if the secret holds only a password')
        s.add_argument('--region', default=os.environ.get('AWS_REGION'))
        s.add_argument('--ca', default=RDS_CA, help='CA bundle for the database TLS check')
        if name == 'changes':
            s.add_argument('--updates', type=int, default=200)
            s.add_argument('--inserts', type=int, default=100)
            s.add_argument('--deletes', type=int, default=50)
            s.add_argument('--seed', type=int)
        else:
            s.add_argument('--cache-host', required=True, help='the cache primary or configuration endpoint')
            s.add_argument('--cache-port', type=int, default=6379)
            s.add_argument('--cache-secret-arn', help='if the cache needs an auth token or user')
            s.add_argument('--key-prefix', default='', help='the link\'s advanced.key_prefix, if set')
            s.add_argument('--wait', type=int, default=30,
                           help='keep checking for up to this many seconds (default 30)')
            s.add_argument('--show-values', action='store_true',
                           help='print source and cache values of mismatches')
    args = p.parse_args()
    if not args.region:
        sys.exit('set AWS_REGION or pass --region')
    if not os.path.isfile(args.ca):
        sys.exit('no CA bundle at {}: run `make ca-bundle` in the repository folder '
                 '(downloads Amazon RDS\'s CA bundle and checks it), or pass --ca'.format(args.ca))
    if args.schema and not SAFE_NAME.match(args.schema):
        sys.exit('--schema: letters, digits and _ only')
    if args.cmd == 'changes':
        cmd_changes(args)
        return 0
    return cmd_compare(args)


if __name__ == '__main__':
    sys.exit(main())
