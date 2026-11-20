from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import sqlite3
import os
import json
import re
import tempfile
import io
from datetime import datetime
import time

app = Flask(__name__)
# Request bodies are refused (413) above 1 MiB. Every API body is a small JSON
# document or link file; without a limit Flask read any size into memory (Jira story F159).
# The database restore, the one upload, sets its own larger limit.
MAX_REQUEST_BYTES = 1024 * 1024
RESTORE_MAX_BYTES = 32 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_BYTES


@app.errorhandler(413)
def _request_too_large(_exc):
    return jsonify({'error': 'the request body is too large'}), 413

# Cross-origin access only for the UI's own addresses (auth.allowed_origins()).
# It used to be CORS(app), which let any web page read every API response.
import auth as _auth                                      # noqa: E402
CORS(app, origins=list(_auth.allowed_origins()))

# --- Authentication ---------------------------------------------------------
# Installed BEFORE any blueprint so the hook covers every route, including the
# ones registered below and any added later. This is not optional hardening: the
# instance role can now create CloudFormation stacks, ECS services and IAM roles,
# so POST /api/links/deploy spends money. Writes are refused unless a token is
# configured and presented. See backend/auth.py for the reasoning.
#
# NOT wrapped in try/except, unlike the blueprints below. A control plane that
# silently starts WITHOUT its authentication because an import failed is worse
# than one that does not start at all.
_auth.install(app)


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    """Whether a token is required, so the UI can ask for one up front.

    Never returns the token itself -- only whether one is required and whether
    the server has one configured.
    """
    return jsonify(_auth.status())

# --- Pipeline Monitor page (/api/monitor/*) ---
# Read-only collectors for the ECDL demo pipeline. Registered defensively: a
# missing optional dependency must not stop the control plane from starting.
try:
    from monitor import monitor_bp
    app.register_blueprint(monitor_bp)
except Exception as _e:  # noqa: BLE001
    print(f"[monitor] pipeline monitor endpoints unavailable: {_e}")

# --- Link configuration (/api/links/*) ---
# Validation and the schema itself come from `ecdl_link` at the repo root, which
# the CLI imports too, so a config one accepts is a config the other accepts.
try:
    from links import links_bp
    app.register_blueprint(links_bp)
    print("[links] link config endpoints registered at /api/links/*")
except Exception as _e:  # noqa: BLE001
    # Keeping the control plane up is right -- the rest of it still works. But
    # leaving the routes simply absent means the UI gets a bare 404 and the
    # operator has to find this line in the container log to learn why. That
    # happened: `ecdl_link` lives at the repo ROOT and the container only mounts
    # `backend/`, so every /api/links call 404'd with nothing pointing at the
    # cause. So the routes still exist, and they return an error that explains the cause.
    print(f"[links] link config endpoints unavailable: {_e}")

    from flask import Blueprint as _Blueprint

    _reason = str(_e)
    _links_unavailable = _Blueprint('links_unavailable', __name__,
                                    url_prefix='/api/links')

    @_links_unavailable.route('/<path:_ignored>',
                              methods=['GET', 'POST', 'PUT', 'DELETE'])
    def _links_unavailable_handler(_ignored):
        return jsonify({
            'error': 'the link configuration API is not available in this '
                     'control plane',
            'reason': _reason,
            'hint': 'The `ecdl_link` package must be importable by the backend. '
                    'It lives at the repository root, so a container that '
                    'mounts only backend/ cannot see it -- mount ecdl_link/ at '
                    '/app/ecdl_link, or install it into the image.',
        }), 503

    app.register_blueprint(_links_unavailable)


# Rate limiting
from functools import wraps
_rate_limit_store = {}

def rate_limit(max_requests=60, window_seconds=60):
    """Simple in-memory rate limiter per IP."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or 'unknown'
            now = time.time()
            key = f"{ip}:{f.__name__}"
            if key not in _rate_limit_store:
                _rate_limit_store[key] = []
            # Clean old entries
            _rate_limit_store[key] = [t for t in _rate_limit_store[key] if now - t < window_seconds]
            if len(_rate_limit_store[key]) >= max_requests:
                return jsonify({'error': 'Rate limit exceeded. Try again later.'}), 429
            _rate_limit_store[key].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

DATABASE_PATH = os.getenv('DATABASE_PATH', '/app/data/app.db')

# --- Input Validation ---
import re

import boto3  # noqa: E402  (Secrets Manager, for source credentials)

# ── Source credentials (Jira story F153) ──────────────────────────────────────────────
# A source on the superseded Data Sources page names a Secrets Manager secret
# (password_secret_arn) that the operator owns, the way a link does. The
# password is read from it when the server connects and is never stored here.
#
# Passwords used to be kept in plain text in this SQLite file. Before that they
# were also copied to a secret named ecdl/sources/<name>, which overwrote any
# secret of that name, was never read and was never deleted. Now a request
# that carries a password is refused (400) rather than stored, init_db() blanks
# any password an older version stored, and a source with no secret cannot
# connect: there is no fallback to a stored password.
#
# The secret may hold JSON with username and password (the RDS shape) or just
# the password. The control plane's role can read it only if it is tagged
# ecdl:link or listed in PipelineSecretArns, the same rule as for links.
SECRET_ARN_PATTERN = re.compile(
    r'^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}$')

PASSWORD_NOT_STORED = (  # nosec B105 - an error message, not a password
    'source passwords are not stored by the control plane. Put the password in '
    'AWS Secrets Manager and send the secret ARN as password_secret_arn.')


class SourceCredentialError(Exception):
    """A source's password could not be obtained. The message says why."""


def validate_secret_arn(value):
    """(arn, error). '' is allowed and means "no secret"."""
    if value is None or value == '':
        return '', None
    if not isinstance(value, str) or not SECRET_ARN_PATTERN.match(value.strip()):
        return None, ('password_secret_arn must be a Secrets Manager secret ARN '
                      '(arn:aws:secretsmanager:<region>:<account>:secret:<name>)')
    return value.strip(), None


def source_credentials(src):
    """(username, password) for a source row, read from its secret.

    Raises SourceCredentialError when the source has no secret or the secret
    cannot be read. Never falls back to anything stored in this database.
    """
    arn = (src.get('password_secret_arn') or '').strip()
    if not arn:
        raise SourceCredentialError(
            'source {!r} has no password secret. Edit it and set the Secrets '
            'Manager ARN that holds its password.'.format(src.get('name')))
    if not SECRET_ARN_PATTERN.match(arn):
        raise SourceCredentialError(
            'source {!r} has an invalid password_secret_arn'.format(src.get('name')))
    from types import SimpleNamespace
    from ecdl_link.tables import read_credential
    try:
        client = boto3.client('secretsmanager', region_name=arn.split(':')[3])
        username, password = read_credential(SimpleNamespace(secretsmanager=client), arn)
    except Exception as exc:  # noqa: BLE001 -- reported to the caller as is
        raise SourceCredentialError(str(exc))
    return (username or src.get('username')), password


def validate_name(name, max_len=200):
    """Validate a name field — alphanumeric, hyphens, underscores, spaces, dots."""
    if not name or not isinstance(name, str):
        return False, "Name is required"
    if len(name) > max_len:
        return False, f"Name must be {max_len} characters or less"
    if not re.match(r'^[a-zA-Z0-9\s\-_\.]+$', name):
        return False, "Name contains invalid characters (use letters, numbers, spaces, hyphens, underscores, dots)"
    return True, None

def validate_host(host):
    """Validate a hostname or IP address."""
    if not host or not isinstance(host, str):
        return False, "Host is required"
    if len(host) > 500:
        return False, "Host is too long"
    if not re.match(r'^[a-zA-Z0-9\.\-:]+$', host):
        return False, "Host contains invalid characters"
    return True, None

def default_port_for_engine(engine, fallback=5432):
    """Default port for a database engine.

    Delegates to ecdl_link.resolve, which holds the per-engine facts, so there is
    one table rather than a literal at each call site. The previous code passed
    5432 regardless of engine, so a MySQL source saved with a blank port was
    stored pointing at the PostgreSQL port.
    """
    try:
        from ecdl_link.resolve import default_port
    except Exception:  # noqa: BLE001 -- the control plane must still start
        return fallback
    return default_port(engine) or fallback


def validate_port(port, default=5432):
    """Validate a port number."""
    try:
        p = int(port) if port else default
        if p < 1 or p > 65535:
            return None, "Port must be between 1 and 65535"
        return p, None
    except (ValueError, TypeError):
        return None, "Port must be a number"

def validate_required_fields(data, fields):
    """Check that required fields are present and non-empty."""
    missing = [f for f in fields if not data.get(f)]
    if missing:
        return False, f"Missing required fields: {', '.join(missing)}"
    return True, None

def audit_log(action, resource_type, resource_id=None, resource_name=None, details=None):
    """Write an entry to the audit log."""
    try:
        conn = get_db()
        conn.execute('''
            INSERT INTO audit_log (timestamp, action, resource_type, resource_id, resource_name, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            action, resource_type, resource_id, resource_name,
            details, request.remote_addr if request else None
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Audit log error: {e}")


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()

    # Data Sources (RDBMS endpoints)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            engine TEXT NOT NULL DEFAULT 'postgres',
            host TEXT NOT NULL,
            port INTEGER DEFAULT 5432,
            database_name TEXT NOT NULL,
            schema_name TEXT DEFAULT 'public',
            username TEXT,
            password TEXT,
            ssl_mode TEXT DEFAULT 'require',
            status TEXT DEFAULT 'new',
            created DATE NOT NULL,
            notes TEXT
        )
    ''')

    # Debezium Connectors
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS connectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            connector_type TEXT NOT NULL DEFAULT 'debezium-postgres',
            tables TEXT,
            snapshot_mode TEXT DEFAULT 'initial',
            slot_name TEXT,
            publication_name TEXT,
            transforms TEXT,
            config_json TEXT,
            status TEXT DEFAULT 'new',
            created DATE NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(id)
        )
    ''')

    # Message Brokers (Kafka / Kinesis)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS brokers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            broker_type TEXT NOT NULL DEFAULT 'kafka',
            bootstrap_servers TEXT,
            auth_type TEXT DEFAULT 'none',
            stream_name TEXT,
            region TEXT DEFAULT 'us-east-1',
            partitions INTEGER DEFAULT 3,
            retention_hours INTEGER DEFAULT 168,
            config_json TEXT,
            status TEXT DEFAULT 'new',
            created DATE NOT NULL,
            notes TEXT
        )
    ''')

    # Valkey Targets (ElastiCache endpoints)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cluster_endpoint TEXT NOT NULL,
            port INTEGER DEFAULT 6379,
            auth_token TEXT,
            ssl_enabled INTEGER DEFAULT 1,
            cluster_mode INTEGER DEFAULT 1,
            key_prefix TEXT DEFAULT '',
            key_pattern TEXT DEFAULT '{table}:{pk}',
            write_mode TEXT DEFAULT 'hset',
            pipeline_batch_size INTEGER DEFAULT 100,
            lww_enabled INTEGER DEFAULT 1,
            sink_template TEXT DEFAULT 'valkey_default',
            config_json TEXT,
            status TEXT DEFAULT 'new',
            created DATE NOT NULL,
            notes TEXT,
            dev_test INTEGER DEFAULT 0
        )
    ''')

    # Add sink_template column if missing (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE targets ADD COLUMN sink_template TEXT DEFAULT 'valkey_default'")
    except Exception:
        pass  # Column already exists

    # Add dev_test column if missing (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE targets ADD COLUMN dev_test INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists

    # Add sink_replicas column if missing (migration for scaling)
    try:
        cursor.execute("ALTER TABLE targets ADD COLUMN sink_replicas INTEGER DEFAULT 1")
    except Exception:
        pass  # Column already exists

    # Add auth_type column to brokers if missing (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE brokers ADD COLUMN auth_type TEXT DEFAULT 'none'")
    except Exception:
        pass  # Column already exists

    # Add vpc_id and subnet_ids columns to deployments if missing (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE deployments ADD COLUMN vpc_id TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE deployments ADD COLUMN subnet_ids TEXT DEFAULT ''")
    except Exception:
        pass

    # Add password_secret_arn column to sources if missing (migration for Secrets Manager)
    try:
        cursor.execute("ALTER TABLE sources ADD COLUMN password_secret_arn TEXT DEFAULT ''")
    except Exception:
        pass

    # Audit log table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id INTEGER,
            resource_name TEXT,
            details TEXT,
            ip_address TEXT
        )
    ''')

    # Pipelines (ties source -> connector -> broker -> target)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pipelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            connector_id INTEGER NOT NULL,
            broker_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            status TEXT DEFAULT 'draft',
            created DATE NOT NULL,
            last_deployed DATE,
            notes TEXT,
            FOREIGN KEY (source_id) REFERENCES sources(id),
            FOREIGN KEY (connector_id) REFERENCES connectors(id),
            FOREIGN KEY (broker_id) REFERENCES brokers(id),
            FOREIGN KEY (target_id) REFERENCES targets(id)
        )
    ''')

    # Deployments (deployment history)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            deploy_type TEXT DEFAULT 'ecs',
            region TEXT DEFAULT 'us-east-1',
            vpc_id TEXT DEFAULT '',
            subnet_ids TEXT DEFAULT '',
            started DATE,
            completed DATE,
            log TEXT,
            config_snapshot TEXT,
            FOREIGN KEY (pipeline_id) REFERENCES pipelines(id)
        )
    ''')

    # Settings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            flag INTEGER,
            i_value INTEGER,
            s_value TEXT,
            f_value REAL
        )
    ''')

    # Blank any source password an older version stored in plain text (Jira story F153).
    # Runs on every start and after a restore, so no copy of one survives here.
    # Those sources need a password_secret_arn before they can connect again.
    cursor.execute("UPDATE sources SET password = NULL "
                   "WHERE password IS NOT NULL AND password != ''")
    if cursor.rowcount:
        print('[sources] removed {} stored source password(s); set a '
              'password_secret_arn on those sources to use them'.format(cursor.rowcount))

    conn.commit()
    conn.close()


# ============================================================
# SOURCES CRUD
# ============================================================

# ── Stored credentials are write-only ────────────────────────────────────────
# Source passwords and target auth tokens are stored in this database and used
# by the server. They are never returned: list responses carry `has_password` /
# `has_auth_token` instead, and an update that leaves the field empty keeps
# the stored value.
SECRET_COLUMNS = {
    'sources': ('password',),
    'targets': ('auth_token',),
}


def _public_row(row, table):
    """A stored row as a dict, with its secret columns replaced by has_<column>."""
    item = dict(row)
    for column in SECRET_COLUMNS.get(table, ()):
        item['has_' + column] = bool(item.pop(column, None))
    if table == 'sources':
        # Only a secret can supply a password now (Jira story F153).
        item['has_password'] = bool(item.get('password_secret_arn'))
    return item


@app.route('/api/sources', methods=['GET'])
def get_sources():
    conn = get_db()
    rows = conn.execute('SELECT * FROM sources ORDER BY name').fetchall()
    conn.close()
    return jsonify([_public_row(r, 'sources') for r in rows])


@app.route('/api/sources', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_source():
    try:
        data = request.json
        # Validate inputs
        ok, err = validate_name(data.get('name'))
        if not ok: return jsonify({'error': err}), 400
        ok, err = validate_host(data.get('host'))
        if not ok: return jsonify({'error': err}), 400
        port, err = validate_port(
            data.get('port'),
            default_port_for_engine(data.get('engine', 'postgres')))
        if err: return jsonify({'error': err}), 400
        ok, err = validate_required_fields(data, ['name', 'host', 'database_name'])
        if not ok: return jsonify({'error': err}), 400
        if data.get('password'):
            return jsonify({'error': PASSWORD_NOT_STORED}), 400
        secret_arn, err = validate_secret_arn(data.get('password_secret_arn'))
        if err: return jsonify({'error': err}), 400

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO sources (name, engine, host, port, database_name, schema_name,
                                 username, password, password_secret_arn, ssl_mode, status, created, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('engine', 'postgres'), data.get('host'),
            port, data.get('database_name'), data.get('schema_name', 'public'),
            data.get('username'), secret_arn,
            data.get('ssl_mode', 'require'),
            data.get('status', 'new'), datetime.now().strftime('%Y-%m-%d'), data.get('notes')
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        audit_log('create', 'source', new_id, data.get('name'), f"engine={data.get('engine')}, host={data.get('host')}")
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        print(f"Error creating source: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sources/<int:id>', methods=['PUT'])
def update_source(id):
    try:
        data = request.json
        if data.get('password'):
            return jsonify({'error': PASSWORD_NOT_STORED}), 400
        # Absent: keep the current secret. '' clears it.
        secret_arn = None
        if 'password_secret_arn' in data:
            secret_arn, err = validate_secret_arn(data.get('password_secret_arn'))
            if err: return jsonify({'error': err}), 400
        conn = get_db()
        conn.execute('''
            UPDATE sources SET name=?, engine=?, host=?, port=?, database_name=?,
                schema_name=?, username=?, password=NULL,
                password_secret_arn=COALESCE(?, password_secret_arn),
                ssl_mode=?, status=?, notes=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('engine'), data.get('host'), data.get('port'),
            data.get('database_name'), data.get('schema_name'), data.get('username'),
            secret_arn, data.get('ssl_mode'), data.get('status'), data.get('notes'), id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sources/<int:id>', methods=['DELETE'])
def delete_source(id):
    conn = get_db()
    conn.execute('DELETE FROM sources WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/sources/<int:id>/scan-tables', methods=['GET'])
def scan_source_tables(id):
    """Connect to a data source and list all available tables."""
    try:
        conn = get_db()
        source = conn.execute('SELECT * FROM sources WHERE id=?', (id,)).fetchone()
        conn.close()

        if not source:
            return jsonify({'error': 'Source not found'}), 404

        s = dict(source)
        engine = s.get('engine', 'postgres')
        tables = []
        if engine in ('postgres', 'mysql'):
            try:
                username, password = source_credentials(s)
            except SourceCredentialError as exc:
                return jsonify({'error': str(exc)}), 400

        if engine == 'postgres':
            import psycopg2
            pg_conn = psycopg2.connect(
                host=s['host'], port=s.get('port', 5432),
                dbname=s['database_name'], user=username,
                password=password,
                sslmode=s.get('ssl_mode', 'require'),
                connect_timeout=10
            )
            cur = pg_conn.cursor()
            cur.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
            """)
            tables = [{'schema': r[0], 'table': r[1]} for r in cur.fetchall()]
            cur.close()
            pg_conn.close()

        elif engine == 'mysql':
            import pymysql
            my_conn = pymysql.connect(
                host=s['host'], port=s.get('port', 3306),
                database=s['database_name'], user=username,
                password=password,
                connect_timeout=10
            )
            cur = my_conn.cursor()
            cur.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE'
                  AND table_schema = %s
                ORDER BY table_name
            """, (s['database_name'],))
            tables = [{'schema': r[0], 'table': r[1]} for r in cur.fetchall()]
            cur.close()
            my_conn.close()

        else:
            return jsonify({'error': f'Table scanning not supported for engine: {engine}'}), 400

        return jsonify({'success': True, 'tables': tables, 'engine': engine})
    except Exception as e:
        print(f"Error scanning tables: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================
# CONNECTORS CRUD
# ============================================================

@app.route('/api/connectors', methods=['GET'])
def get_connectors():
    conn = get_db()
    rows = conn.execute('''
        SELECT c.*, s.name as source_name, s.engine as source_engine
        FROM connectors c LEFT JOIN sources s ON c.source_id = s.id
        ORDER BY c.name
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/connectors', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_connector():
    try:
        data = request.json
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO connectors (name, source_id, connector_type, tables, snapshot_mode,
                                    slot_name, publication_name, transforms, config_json, status, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('source_id'), data.get('connector_type', 'debezium-postgres'),
            data.get('tables'), data.get('snapshot_mode', 'initial'),
            data.get('slot_name'), data.get('publication_name'),
            data.get('transforms'), data.get('config_json'),
            data.get('status', 'new'), datetime.now().strftime('%Y-%m-%d')
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/connectors/<int:id>', methods=['PUT'])
def update_connector(id):
    try:
        data = request.json
        conn = get_db()
        conn.execute('''
            UPDATE connectors SET name=?, source_id=?, connector_type=?, tables=?,
                snapshot_mode=?, slot_name=?, publication_name=?, transforms=?,
                config_json=?, status=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('source_id'), data.get('connector_type'),
            data.get('tables'), data.get('snapshot_mode'), data.get('slot_name'),
            data.get('publication_name'), data.get('transforms'),
            data.get('config_json'), data.get('status'), id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/connectors/<int:id>', methods=['DELETE'])
def delete_connector(id):
    conn = get_db()
    conn.execute('DELETE FROM connectors WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# BROKERS CRUD
# ============================================================

@app.route('/api/brokers', methods=['GET'])
def get_brokers():
    conn = get_db()
    rows = conn.execute('SELECT * FROM brokers ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/brokers', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_broker():
    try:
        data = request.json
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO brokers (name, broker_type, bootstrap_servers, auth_type, stream_name,
                                 region, partitions, retention_hours, config_json, status, created, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('broker_type', 'kafka'),
            data.get('bootstrap_servers'), data.get('auth_type', 'none'),
            data.get('stream_name'),
            data.get('region', 'us-east-1'), data.get('partitions', 3),
            data.get('retention_hours', 168), data.get('config_json'),
            data.get('status', 'new'), datetime.now().strftime('%Y-%m-%d'), data.get('notes')
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/brokers/<int:id>', methods=['PUT'])
def update_broker(id):
    try:
        data = request.json
        conn = get_db()
        conn.execute('''
            UPDATE brokers SET name=?, broker_type=?, bootstrap_servers=?, auth_type=?, stream_name=?,
                region=?, partitions=?, retention_hours=?, config_json=?, status=?, notes=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('broker_type'), data.get('bootstrap_servers'),
            data.get('auth_type', 'none'),
            data.get('stream_name'), data.get('region'), data.get('partitions'),
            data.get('retention_hours'), data.get('config_json'),
            data.get('status'), data.get('notes'), id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/brokers/<int:id>', methods=['DELETE'])
def delete_broker(id):
    conn = get_db()
    conn.execute('DELETE FROM brokers WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# TARGETS CRUD
# ============================================================

@app.route('/api/targets', methods=['GET'])
def get_targets():
    conn = get_db()
    rows = conn.execute('SELECT * FROM targets ORDER BY name').fetchall()
    conn.close()
    return jsonify([_public_row(r, 'targets') for r in rows])


@app.route('/api/targets', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_target():
    try:
        data = request.json
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO targets (name, cluster_endpoint, port, auth_token, ssl_enabled,
                                  cluster_mode, key_prefix, key_pattern, write_mode,
                                  pipeline_batch_size, lww_enabled, sink_template,
                                  config_json, status, created, notes, dev_test, sink_replicas)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('cluster_endpoint'), data.get('port', 6379),
            data.get('auth_token'), data.get('ssl_enabled', 1), data.get('cluster_mode', 1),
            data.get('key_prefix', ''), data.get('key_pattern', '{table}:{pk}'),
            data.get('write_mode', 'hset'), data.get('pipeline_batch_size', 100),
            data.get('lww_enabled', 1), data.get('sink_template', 'valkey_default'),
            data.get('config_json'),
            data.get('status', 'new'), datetime.now().strftime('%Y-%m-%d'), data.get('notes'),
            data.get('dev_test', 0), data.get('sink_replicas', 1)
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/targets/<int:id>', methods=['PUT'])
def update_target(id):
    try:
        data = request.json
        conn = get_db()
        conn.execute('''
            UPDATE targets SET name=?, cluster_endpoint=?, port=?,
                auth_token=COALESCE(NULLIF(?, ''), auth_token),
                ssl_enabled=?, cluster_mode=?, key_prefix=?, key_pattern=?,
                write_mode=?, pipeline_batch_size=?, lww_enabled=?,
                sink_template=?, config_json=?, status=?, notes=?, dev_test=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('cluster_endpoint'), data.get('port'),
            data.get('auth_token'), data.get('ssl_enabled'), data.get('cluster_mode'),
            data.get('key_prefix'), data.get('key_pattern'), data.get('write_mode'),
            data.get('pipeline_batch_size'), data.get('lww_enabled'),
            data.get('sink_template', 'valkey_default'),
            data.get('config_json'), data.get('status'), data.get('notes'),
            data.get('dev_test', 0), id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/targets/<int:id>', methods=['DELETE'])
def delete_target(id):
    conn = get_db()
    conn.execute('DELETE FROM targets WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# PIPELINES CRUD
# ============================================================

@app.route('/api/pipelines', methods=['GET'])
def get_pipelines():
    conn = get_db()
    rows = conn.execute('''
        SELECT p.*,
               s.name as source_name, s.engine as source_engine,
               c.name as connector_name,
               b.name as broker_name, b.broker_type,
               t.name as target_name
        FROM pipelines p
        LEFT JOIN sources s ON p.source_id = s.id
        LEFT JOIN connectors c ON p.connector_id = c.id
        LEFT JOIN brokers b ON p.broker_id = b.id
        LEFT JOIN targets t ON p.target_id = t.id
        ORDER BY p.name
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/pipelines', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_pipeline():
    try:
        data = request.json
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pipelines (name, source_id, connector_id, broker_id, target_id,
                                    status, created, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('source_id'), data.get('connector_id'),
            data.get('broker_id'), data.get('target_id'),
            data.get('status', 'draft'), datetime.now().strftime('%Y-%m-%d'), data.get('notes')
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>', methods=['PUT'])
def update_pipeline(id):
    try:
        data = request.json
        conn = get_db()
        conn.execute('''
            UPDATE pipelines SET name=?, source_id=?, connector_id=?, broker_id=?,
                target_id=?, status=?, notes=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('source_id'), data.get('connector_id'),
            data.get('broker_id'), data.get('target_id'),
            data.get('status'), data.get('notes'), id
        ))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>', methods=['DELETE'])
def delete_pipeline(id):
    conn = get_db()
    conn.execute('DELETE FROM pipelines WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ============================================================
# DEPLOYMENTS
# ============================================================

@app.route('/api/deployments', methods=['GET'])
def get_deployments():
    conn = get_db()
    rows = conn.execute('''
        SELECT d.*, p.name as pipeline_name
        FROM deployments d
        LEFT JOIN pipelines p ON d.pipeline_id = p.id
        ORDER BY d.id DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/deployments', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=60)
def create_deployment():
    try:
        data = request.json
        pipeline_id = data.get('pipeline_id')

        # Build config snapshot from pipeline
        conn = get_db()
        pipeline = conn.execute('''
            SELECT p.*, s.name as source_name, s.engine, s.host, s.port as src_port,
                   s.database_name, s.schema_name, s.username,
                   c.connector_type, c.tables, c.snapshot_mode, c.slot_name, c.publication_name,
                   b.broker_type, b.bootstrap_servers, b.auth_type, b.stream_name, b.region as broker_region,
                   b.partitions,
                   t.cluster_endpoint, t.port as tgt_port, t.key_prefix, t.key_pattern,
                   t.write_mode, t.pipeline_batch_size, t.lww_enabled
            FROM pipelines p
            LEFT JOIN sources s ON p.source_id = s.id
            LEFT JOIN connectors c ON p.connector_id = c.id
            LEFT JOIN brokers b ON p.broker_id = b.id
            LEFT JOIN targets t ON p.target_id = t.id
            WHERE p.id = ?
        ''', (pipeline_id,)).fetchone()

        if not pipeline:
            conn.close()
            return jsonify({'error': 'Pipeline not found'}), 404

        config_snapshot = json.dumps(dict(pipeline))
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO deployments (pipeline_id, status, deploy_type, region, vpc_id, subnet_ids, started, config_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            pipeline_id, 'pending', data.get('deploy_type', 'ecs'),
            data.get('region', 'us-east-1'), data.get('vpc_id', ''), data.get('subnet_ids', ''),
            now, config_snapshot
        ))

        # Update pipeline status
        conn.execute("UPDATE pipelines SET status='deploying', last_deployed=? WHERE id=?",
                     (now, pipeline_id))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'success': True}), 201
    except Exception as e:
        print(f"Error creating deployment: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/deployments/<int:id>/status', methods=['PUT'])
def update_deployment_status(id):
    try:
        data = request.json
        new_status = data.get('status')
        conn = get_db()

        # One fixed statement: a NULL bind leaves that column as it was.
        completed = (datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                     if new_status in ('running', 'error', 'stopped') else None)
        conn.execute(
            'UPDATE deployments SET status=?, completed=COALESCE(?, completed), '
            'log=COALESCE(?, log) WHERE id=?',
            (new_status, completed, data.get('log') or None, id))

        # Also update pipeline status
        deployment = conn.execute('SELECT pipeline_id FROM deployments WHERE id=?', (id,)).fetchone()
        if deployment:
            pipeline_status = 'running' if new_status == 'running' else 'deployed' if new_status == 'stopped' else new_status
            conn.execute('UPDATE pipelines SET status=? WHERE id=?',
                         (pipeline_status, deployment['pipeline_id']))

        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# SETTINGS
# ============================================================

@app.route('/api/settings/<key>', methods=['GET'])
def get_setting(key):
    try:
        conn = get_db()
        setting = conn.execute('SELECT * FROM settings WHERE key = ?', (key,)).fetchone()
        conn.close()
        if setting:
            result = dict(setting)
            if result.get('flag') is not None:
                result['flag'] = bool(result['flag'])
            return jsonify(result), 200
        return jsonify({'key': key, 'flag': None, 'i_value': None, 's_value': None, 'f_value': None}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings', methods=['POST'])
def update_setting():
    try:
        data = request.json
        key = data.get('key')
        if not key:
            return jsonify({'error': 'Missing key'}), 400

        flag_value = data.get('flag')
        if flag_value is not None:
            flag_value = 1 if flag_value else 0

        conn = get_db()
        existing = conn.execute('SELECT * FROM settings WHERE key = ?', (key,)).fetchone()

        if existing:
            conn.execute('UPDATE settings SET flag=?, i_value=?, s_value=?, f_value=? WHERE key=?',
                         (flag_value, data.get('i_value'), data.get('s_value'), data.get('f_value'), key))
        else:
            conn.execute('INSERT INTO settings (key, flag, i_value, s_value, f_value) VALUES (?, ?, ?, ?, ?)',
                         (key, flag_value, data.get('i_value'), data.get('s_value'), data.get('f_value')))

        conn.commit()
        conn.close()
        return jsonify({'success': True}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# MAINTENANCE
# ============================================================

@app.route('/api/maintenance/backup', methods=['GET'])
def backup_database():
    try:
        if not os.path.exists(DATABASE_PATH):
            return jsonify({'error': 'Database file not found'}), 404
        # A copy with every stored credential blanked, built in memory: the
        # backup is a download, and a download must not carry passwords.
        # Restoring it keeps the configuration; credentials are re-entered.
        src = sqlite3.connect(DATABASE_PATH)
        copy = sqlite3.connect(':memory:')
        try:
            src.backup(copy)
        finally:
            src.close()
        try:
            # One fixed statement per column in SECRET_COLUMNS.
            copy.execute('UPDATE sources SET password = NULL')
            copy.execute('UPDATE targets SET auth_token = NULL')
            copy.commit()
            fd, path = tempfile.mkstemp(suffix='.db')
            os.close(fd)
            out = sqlite3.connect(path)
            try:
                copy.backup(out)
            finally:
                out.close()
        finally:
            copy.close()
        with open(path, 'rb') as fh:
            data = fh.read()
        os.remove(path)
        return send_file(io.BytesIO(data), as_attachment=True, download_name='app.db',
                         mimetype='application/octet-stream')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# What a restored file must contain to be this application's database.
RESTORE_REQUIRED_TABLES = ('sources', 'connectors', 'brokers', 'targets',
                           'pipelines', 'deployments', 'settings')
SQLITE_HEADER = b'SQLite format 3\x00'


def _restore_problem(path):
    """Why the SQLite file at `path` cannot be restored, or None if it can.

    It must be an intact SQLite database with this application's tables, and
    no triggers or views: the database it replaces has none, and a trigger
    would run inside the control plane's own statements (Jira story F158).
    """
    with open(path, 'rb') as fh:
        if fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            return 'not a SQLite database'
    from urllib.parse import quote
    try:
        conn = sqlite3.connect('file:{}?mode=ro'.format(quote(path)), uri=True)
    except sqlite3.Error as exc:
        return 'cannot open the database: {}'.format(exc)
    try:
        check = [row[0] for row in conn.execute('PRAGMA integrity_check').fetchall()]
        if check != ['ok']:
            return 'the database failed its integrity check: {}'.format('; '.join(check[:3]))
        kinds = {}
        for kind, name in conn.execute('SELECT type, name FROM sqlite_master'):
            kinds.setdefault(kind, []).append(name)
    except sqlite3.Error as exc:
        return 'cannot read the database: {}'.format(exc)
    finally:
        conn.close()
    missing = [t for t in RESTORE_REQUIRED_TABLES if t not in kinds.get('table', ())]
    if missing:
        return 'not an ECDL control-plane database (no {} table)'.format(', '.join(missing))
    extra = sorted(kinds.get('trigger', []) + kinds.get('view', []))
    if extra:
        return 'the database has triggers or views, which ECDL never creates: {}'.format(
            ', '.join(extra))
    return None


@app.route('/api/maintenance/restore', methods=['POST'])
def restore_database():
    # A larger limit than other requests: this is a whole database file.
    request.max_content_length = RESTORE_MAX_BYTES
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    # A private temporary file beside the database, so the final rename is
    # atomic and two restores cannot write the same path.
    fd, temp_path = tempfile.mkstemp(prefix='.restore-', suffix='.db',
                                     dir=os.path.dirname(DATABASE_PATH) or '.')
    os.close(fd)
    try:
        file.save(temp_path)
        problem = _restore_problem(temp_path)
        if problem:
            return jsonify({'error': 'Invalid database: ' + problem}), 400
        os.replace(temp_path, DATABASE_PATH)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    # Bring an older backup up to the current schema, and blank any stored
    # source password it carries (see init_db).
    init_db()
    audit_log('restore', 'database')
    return jsonify({'success': True})


# ============================================================
# RANDOMIZER – data generator for testing
# ============================================================

import threading
import random
import string
import time as _time

import repo_path  # noqa: E402,F401  (puts ecdl_link on sys.path)
# `ecdl_link` is imported inside the functions that use it, not here. A
# module-level import made app.py fail to load whenever ecdl_link was missing,
# so the "link configuration API is not available" routes registered above
# could never be served: the control plane did not start at all (Jira story F166).

_randomizer_threads = {}   # keyed by source_id
_randomizer_stop = {}      # Event flags

# The Randomizer writes generated rows into the customer's source database. It
# is not ready, so the UI hides it and its API is off unless the control plane
# is started with ECDL_ENABLE_RANDOMIZER=true (Jira story F154). That covers /api/randomizer/*
# and the two source endpoints only it uses (/tables and /columns). When it is
# on, every run stops after max_rows insert attempts or max_seconds, whichever
# comes first; the request may lower or raise them up to the caps below.
RANDOMIZER_FLAG = 'ECDL_ENABLE_RANDOMIZER'
RANDOMIZER_DEFAULT_MAX_ROWS = 1000
RANDOMIZER_MAX_ROWS = 100000
RANDOMIZER_DEFAULT_MAX_SECONDS = 300
RANDOMIZER_MAX_SECONDS = 3600


def randomizer_enabled():
    return os.getenv(RANDOMIZER_FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _randomizer_off():
    """A 404 response while the Randomizer is switched off, else None."""
    if randomizer_enabled():
        return None
    return jsonify({
        'error': 'the Randomizer is switched off',
        'hint': 'It writes generated rows into the source database and is not '
                'ready. Start the control plane with {}=true to use it.'.format(
                    RANDOMIZER_FLAG),
    }), 404


def _connect_source_db(src):
    """Return a DB-API connection for the given source row dict.

    The password comes from the source's secret (source_credentials), which
    raises SourceCredentialError when there is none.
    """
    engine = src['engine']
    if engine not in ('postgres', 'mysql'):
        raise ValueError(f"Unsupported engine: {engine}")
    username, password = source_credentials(src)
    if engine == 'postgres':
        import psycopg2
        return psycopg2.connect(
            host=src['host'], port=src['port'],
            dbname=src['database_name'], user=username,
            password=password,
            sslmode=src.get('ssl_mode', 'require'),
            connect_timeout=10
        )
    else:
        import pymysql
        return pymysql.connect(
            host=src['host'], port=src['port'],
            database=src['database_name'], user=username,
            password=password, connect_timeout=10
        )


@app.route('/api/sources/<int:id>/tables', methods=['GET'])
def list_source_tables(id):
    """Connect to the source DB and return its table list. (Randomizer only.)"""
    off = _randomizer_off()
    if off:
        return off
    try:
        conn = get_db()
        src = conn.execute('SELECT * FROM sources WHERE id=?', (id,)).fetchone()
        conn.close()
        if not src:
            return jsonify({'error': 'Source not found'}), 404
        src = dict(src)

        db = _connect_source_db(src)
        cur = db.cursor()
        if src['engine'] == 'postgres':
            schema = src.get('schema_name') or 'public'
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",
                (schema,)
            )
        elif src['engine'] == 'mysql':
            cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]
        cur.close()
        db.close()
        return jsonify({'tables': tables})
    except SourceCredentialError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Error listing tables: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sources/<int:id>/columns', methods=['GET'])
def list_source_columns(id):
    """Return column names + types for a given table. (Randomizer only.)"""
    off = _randomizer_off()
    if off:
        return off
    table = request.args.get('table')
    if not table:
        return jsonify({'error': 'table parameter required'}), 400
    try:
        conn = get_db()
        src = conn.execute('SELECT * FROM sources WHERE id=?', (id,)).fetchone()
        conn.close()
        if not src:
            return jsonify({'error': 'Source not found'}), 404
        src = dict(src)

        db = _connect_source_db(src)
        cur = db.cursor()
        if src['engine'] == 'postgres':
            schema = src.get('schema_name') or 'public'
            cur.execute(
                "SELECT column_name, data_type, is_nullable, column_default, "
                "character_maximum_length, numeric_precision, numeric_scale, udt_name "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, table)
            )
            cols = [{'name': r[0], 'type': r[1], 'nullable': r[2], 'default': r[3],
                     'max_length': r[4], 'precision': r[5], 'scale': r[6],
                     'udt_name': r[7], 'allowed_values': None} for r in cur.fetchall()]

            # Fetch CHECK constraints to find allowed values per column
            cur.execute("""
                SELECT cc.check_clause, ccu.column_name
                FROM information_schema.check_constraints cc
                JOIN information_schema.constraint_column_usage ccu
                  ON cc.constraint_name = ccu.constraint_name
                  AND cc.constraint_schema = ccu.constraint_schema
                WHERE ccu.table_schema = %s AND ccu.table_name = %s
            """, (schema, table))
            import re
            for row in cur.fetchall():
                clause, col_name = row[0], row[1]
                # Extract values from patterns like: (status)::text = ANY (ARRAY['pending'::text, ...])
                # or: ((status)::text = 'value'::text)
                vals = re.findall(r"'([^']*)'(?:::[\w]+)?", clause)
                if vals:
                    # deduplicate and filter out type casts
                    unique_vals = list(dict.fromkeys(v for v in vals if v))
                    for c in cols:
                        if c['name'] == col_name and unique_vals:
                            c['allowed_values'] = unique_vals

            # Fetch ENUM type values for USER-DEFINED columns
            for c in cols:
                if c['type'] == 'USER-DEFINED' and c.get('udt_name'):
                    cur.execute(
                        "SELECT enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON e.enumtypid = t.oid "
                        "WHERE t.typname = %s ORDER BY e.enumsortorder",
                        (c['udt_name'],)
                    )
                    enum_vals = [r[0] for r in cur.fetchall()]
                    if enum_vals:
                        c['allowed_values'] = enum_vals
                        c['type'] = f"enum({c['udt_name']})"

        elif src['engine'] == 'mysql':
            cur.execute(
                "SELECT column_name, column_type, is_nullable, column_default, "
                "character_maximum_length, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (src['database_name'], table)
            )
            import re
            cols = []
            for r in cur.fetchall():
                col = {'name': r[0], 'type': r[1], 'nullable': r[2], 'default': r[3],
                       'max_length': r[4], 'precision': r[5], 'scale': r[6], 'allowed_values': None}
                # MySQL ENUM: column_type looks like "enum('val1','val2',...)"
                if r[1].startswith('enum(') or r[1].startswith("enum('"):
                    vals = re.findall(r"'([^']*)'", r[1])
                    if vals:
                        col['allowed_values'] = vals
                cols.append(col)
        else:
            cols = []
        cur.close()
        db.close()
        return jsonify({'columns': cols})
    except SourceCredentialError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Error listing columns: {e}")
        return jsonify({'error': str(e)}), 500


def _random_value(col_type, max_length=None, precision=None, scale=None, allowed_values=None):
    """Generate a plausible random value for a SQL column type."""
    t = col_type.lower()
    if any(k in t for k in ('serial', 'identity')):
        return None  # auto-generated
    # If there are allowed values (CHECK constraint or ENUM), pick from them
    if allowed_values and len(allowed_values) > 0:
        return random.choice(allowed_values)
    if 'int' in t:
        return random.randint(1, 100000)
    if 'bool' in t:
        return random.choice([True, False])
    if any(k in t for k in ('float', 'double', 'real', 'numeric', 'decimal')):
        if precision and scale:
            int_digits = precision - scale
            max_val = 10 ** int_digits - 1
            return round(random.uniform(0, min(max_val, 10000)), scale)
        return round(random.uniform(0, 10000), 2)
    if 'date' in t and 'time' not in t:
        return f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
    if 'time' in t:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if 'json' in t:
        return json.dumps({'key': ''.join(random.choices(string.ascii_lowercase, k=5))})
    if 'uuid' in t:
        import uuid
        return str(uuid.uuid4())
    # default: text / varchar / char — respect max_length
    if max_length and max_length > 0:
        length = max(1, min(max_length, 50))
    elif 'char' in t or 'text' in t or 'varchar' in t:
        length = 12
    else:
        length = 10
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def _randomizer_worker(source_id, table, columns, batch_size, interval, schema,
                       max_rows=None, max_seconds=None):
    """Background thread that inserts random rows, until stopped or a limit.

    max_rows counts attempts (rows written plus failed inserts), so a table
    that rejects every row stops too instead of retrying indefinitely.
    """
    stop_evt = _randomizer_stop.get(source_id)
    status = _randomizer_threads[source_id]
    max_rows = max_rows or RANDOMIZER_DEFAULT_MAX_ROWS
    max_seconds = max_seconds or RANDOMIZER_DEFAULT_MAX_SECONDS
    deadline = _time.monotonic() + max_seconds

    def limit_reached():
        if status['records_written'] + status['errors'] >= max_rows:
            return 'max_rows'
        if _time.monotonic() >= deadline:
            return 'max_seconds'
        return None
    try:
        conn_app = get_db()
        src = conn_app.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        conn_app.close()
        if not src:
            status['status'] = 'error'
            status['error'] = 'Source not found'
            return
        src = dict(src)
        db = _connect_source_db(src)
        db.autocommit = True if hasattr(db, 'autocommit') else False
        cur = db.cursor()

        # filter out auto-generated columns
        gen_cols = [c for c in columns if _random_value(c['type'], c.get('max_length'), c.get('precision'), c.get('scale'), c.get('allowed_values')) is not None]
        col_names = [c['name'] for c in gen_cols]
        insert_sql = _randomizer_insert_sql(cur, src, schema, table, col_names)

        while not stop_evt.is_set() and not limit_reached():
            for _ in range(batch_size):
                if stop_evt.is_set() or limit_reached():
                    break
                vals = [_random_value(c['type'], c.get('max_length'), c.get('precision'), c.get('scale'), c.get('allowed_values')) for c in gen_cols]
                try:
                    cur.execute(insert_sql, vals)
                    if not getattr(db, 'autocommit', False):
                        db.commit()
                    status['records_written'] += 1
                except Exception as ie:
                    status['errors'] += 1
                    status['last_error'] = str(ie)
                    try:
                        db.rollback()
                    except Exception:
                        pass
            if not limit_reached():
                stop_evt.wait(interval)

        status['stop_reason'] = limit_reached() or 'requested'
        status['status'] = 'stopped'
        cur.close()
        db.close()
    except Exception as e:
        status['status'] = 'error'
        status['error'] = str(e)


def _randomizer_insert_sql(cur, src, schema, table, col_names):
    """The INSERT for the randomizer, with every name checked twice.

    Table, schema and column names arrive in the request body and cannot be bound
    as parameters. They must be plain identifiers (sqlident rejects quotes, ';',
    spaces and the rest), and every column must actually exist in that table
    according to information_schema -- read with bound parameters. Only then are
    they quoted into the statement. Row values are always bound.
    """
    from ecdl_link import sqlident
    engine = src['engine']
    if engine == 'postgres':
        style = 'postgresql'
        cur.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s', (schema, table))
        qualified = '{}.{}'.format(schema, table)
    elif engine == 'mysql':
        style = 'mysql'
        cur.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s', (src['database_name'], table))
        qualified = table
    else:
        raise ValueError('unsupported engine {!r}'.format(engine))
    existing = {str(r[0]) for r in cur.fetchall()}
    if not existing:
        raise ValueError('table {!r} not found in the source'.format(table))
    unknown = [c for c in col_names if c not in existing]
    if unknown:
        raise ValueError('not columns of {}: {}'.format(table, ', '.join(map(repr, unknown))))
    return sqlident.render(style, 'INSERT INTO {t} ({cols}) VALUES ({marks})',
                           t=qualified, cols=col_names,
                           marks=sqlident.binds(len(col_names)))


def _randomizer_request(data):
    """Parse and check the start request. Raises ValueError with a reason."""
    from ecdl_link import sqlident
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object')
    table = sqlident.validate(data.get('table'), 'table')
    schema = sqlident.validate(data.get('schema') or 'public', 'schema')
    columns = data.get('columns')
    if not isinstance(columns, list) or not columns:
        raise ValueError('columns must be a non-empty list')
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get('type'), str):
            raise ValueError('each column needs a name and a type')
        sqlident.validate(col.get('name'), 'column')
    batch_size = int(data.get('batch_size', 1))
    interval = int(data.get('interval', 1))
    if not 1 <= batch_size <= 1000:
        raise ValueError('batch_size must be between 1 and 1000')
    if not 0 <= interval <= 3600:
        raise ValueError('interval must be between 0 and 3600 seconds')
    max_rows = int(data.get('max_rows') or RANDOMIZER_DEFAULT_MAX_ROWS)
    max_seconds = int(data.get('max_seconds') or RANDOMIZER_DEFAULT_MAX_SECONDS)
    if not 1 <= max_rows <= RANDOMIZER_MAX_ROWS:
        raise ValueError('max_rows must be between 1 and {}'.format(RANDOMIZER_MAX_ROWS))
    if not 1 <= max_seconds <= RANDOMIZER_MAX_SECONDS:
        raise ValueError('max_seconds must be between 1 and {}'.format(
            RANDOMIZER_MAX_SECONDS))
    source_id = data.get('source_id')
    if isinstance(source_id, bool) or not isinstance(source_id, int):
        raise ValueError('source_id must be a number')
    return {'source_id': source_id, 'table': table, 'schema': schema,
            'columns': columns, 'batch_size': batch_size, 'interval': interval,
            'max_rows': max_rows, 'max_seconds': max_seconds}


@app.route('/api/randomizer/start', methods=['POST'])
def randomizer_start():
    """Start the background data generator for a source."""
    off = _randomizer_off()
    if off:
        return off
    try:
        try:
            req = _randomizer_request(request.get_json(silent=True))
        except (ValueError, TypeError) as exc:
            return jsonify({'error': str(exc)}), 400
        source_id = req['source_id']
        table = req['table']
        columns = req['columns']        # list of {name, type}
        batch_size = req['batch_size']
        interval = req['interval']
        schema = req['schema']

        # stop any existing thread for this source
        if source_id in _randomizer_stop:
            _randomizer_stop[source_id].set()

        stop_evt = threading.Event()
        _randomizer_stop[source_id] = stop_evt
        status = {
            'status': 'running',
            'records_written': 0,
            'errors': 0,
            'last_error': None,
            'source_id': source_id,
            'table': table,
            'batch_size': batch_size,
            'interval': interval,
            'max_rows': req['max_rows'],
            'max_seconds': req['max_seconds'],
        }
        _randomizer_threads[source_id] = status

        t = threading.Thread(
            target=_randomizer_worker,
            args=(source_id, table, columns, batch_size, interval, schema,
                  req['max_rows'], req['max_seconds']),
            daemon=True
        )
        t.start()
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/randomizer/stop', methods=['POST'])
def randomizer_stop():
    """Stop the background data generator."""
    off = _randomizer_off()
    if off:
        return off
    try:
        source_id = request.json.get('source_id')
        if source_id in _randomizer_stop:
            _randomizer_stop[source_id].set()
        status = _randomizer_threads.get(source_id, {})
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/randomizer/status', methods=['GET'])
def randomizer_status():
    """Return current randomizer status for a source."""
    off = _randomizer_off()
    if off:
        return off
    source_id = request.args.get('source_id', type=int)
    status = _randomizer_threads.get(source_id, {'status': 'idle', 'records_written': 0, 'errors': 0})
    return jsonify(status)


@app.route('/api/audit-log', methods=['GET'])
def get_audit_log():
    """Return recent audit log entries."""
    try:
        limit = request.args.get('limit', 100, type=int)
        conn = get_db()
        rows = conn.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Comprehensive health check for all pipeline components."""
    health = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'backend': {'status': 'healthy'},
        'database': {'status': 'unknown'},
        'components': {}
    }
    # Check SQLite
    try:
        conn = get_db()
        conn.execute('SELECT 1').fetchone()
        conn.close()
        health['database'] = {'status': 'healthy'}
    except Exception as e:
        health['database'] = {'status': 'unhealthy', 'error': str(e)}

    # Check running deployments and their components
    try:
        conn = get_db()
        deploys = conn.execute('''
            SELECT d.id, d.status, d.deploy_type, p.name as pipeline_name
            FROM deployments d
            LEFT JOIN pipelines p ON d.pipeline_id = p.id
            WHERE d.status IN ('running', 'deploying', 'pending')
        ''').fetchall()
        conn.close()
        health['active_deployments'] = len(deploys)
        for d in deploys:
            health['components'][d['pipeline_name'] or f'deployment-{d["id"]}'] = {
                'status': d['status'],
                'deploy_type': d['deploy_type']
            }
    except Exception as e:
        health['active_deployments'] = 0

    # Overall status
    if health['database']['status'] != 'healthy':
        health['overall'] = 'degraded'
    else:
        health['overall'] = 'healthy'

    status_code = 200 if health['overall'] == 'healthy' else 503
    return jsonify(health), status_code


# There used to be a background "pipeline health monitor" here. Every 60 s it
# opened TCP connections to every broker host stored on the superseded Brokers
# page (a port probe of whatever address a user typed in), restarted Kafka
# Connect tasks on localhost:8083, and published DebeziumReachable=0 to
# CloudWatch because no Kafka Connect runs in this architecture. Links are
# watched by the Pipeline Monitor (monitor.py) instead, so it was removed (Jira story F162).

if __name__ == '__main__':
    init_db()
    # Loopback unless told otherwise. The container image sets ECDL_BIND_HOST to
    # all interfaces because a container must accept its published port; the
    # publish (and the security group in front of it) is what limits who reaches
    # it. A bare `python app.py` on a laptop or a server stays private.
    app.run(host=os.environ.get('ECDL_BIND_HOST', '127.0.0.1'), port=5000, debug=False)
