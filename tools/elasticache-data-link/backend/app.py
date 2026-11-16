from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS
import sqlite3
import os
import json
import re
import shutil
import yaml
import tempfile
import zipfile
import io
from datetime import datetime
import time

app = Flask(__name__)
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

# --- AWS Secrets Manager ---
import boto3

def _store_secret(name, username, password):
    """Store credentials in AWS Secrets Manager. Returns secret ARN or None if unavailable."""
    try:
        client = boto3.client('secretsmanager')
        secret_name = f'ecdl/sources/{name}'
        secret_value = json.dumps({'username': username, 'password': password})
        try:
            resp = client.create_secret(Name=secret_name, SecretString=secret_value)
            return resp['ARN']
        except client.exceptions.ResourceExistsException:
            resp = client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
            return client.describe_secret(SecretId=secret_name)['ARN']
    except Exception as e:
        print(f"Secrets Manager unavailable, storing locally: {e}")
        return None

def _get_secret(secret_arn):
    """Retrieve credentials from Secrets Manager. Returns (username, password) or (None, None)."""
    try:
        client = boto3.client('secretsmanager')
        resp = client.get_secret_value(SecretId=secret_arn)
        secret = json.loads(resp['SecretString'])
        return secret.get('username'), secret.get('password')
    except Exception as e:
        print(f"Failed to retrieve secret: {e}")
        return None, None

def _delete_secret(secret_arn):
    """Delete a secret from Secrets Manager."""
    try:
        client = boto3.client('secretsmanager')
        client.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
    except Exception:
        pass

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

    conn.commit()
    conn.close()


# ============================================================
# SOURCES CRUD
# ============================================================

# ── Stored credentials are write-only ────────────────────────────────────────
# Source passwords and target auth tokens are stored (in Secrets Manager when it
# is available, otherwise in this database) and used by the server. They are
# never returned: list responses carry `has_password` / `has_auth_token`
# instead, and an update that leaves the field empty keeps the stored value.
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
        item['has_password'] = item['has_password'] or bool(item.get('password_secret_arn'))
    return item


# ── Superseded pipeline packages ─────────────────────────────────────────────
# The "Superseded by Links" pages could generate and deploy Kafka/MSK packages
# that embed the source password and cache token in plain text in the files
# they return (connector-config.json, environment variables, EC2 user data).
# Those endpoints are off unless ECDL_LEGACY_PACKAGES=true. Links replace them,
# and links never put a credential in a response or a package: the pipeline
# tasks read Secrets Manager themselves. The Smart Builder chat is off for the
# same reason: its tools create those records.
_LEGACY_PACKAGE_PATHS = re.compile(
    r'^/api/(pipelines/\d+/(generate-config|generate-docker-compose|'
    r'download-deployment|generate-ecs-fargate|download-ecs-deployment|'
    r'deploy-local(/status)?|deploy-cloud)|connectors/\d+/apply-changes|'
    r'smart-builder/chat)$')


def legacy_packages_enabled():
    return os.environ.get('ECDL_LEGACY_PACKAGES', '').strip().lower() in ('1', 'true', 'yes')


@app.before_request
def _refuse_legacy_packages():
    if request.method == 'OPTIONS' or legacy_packages_enabled():
        return None
    if _LEGACY_PACKAGE_PATHS.match(request.path):
        return jsonify({
            'error': 'this endpoint is switched off: it generates a package that '
                     'contains database and cache credentials in plain text',
            'hint': 'Use Build Pipeline (links) instead; a link keeps credentials '
                    'in Secrets Manager. An operator can set '
                    'ECDL_LEGACY_PACKAGES=true to switch it back on.',
        }), 410
    return None


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

        conn = get_db()
        cursor = conn.cursor()

        # Store password in Secrets Manager if available
        password = data.get('password', '')
        secret_arn = _store_secret(data.get('name'), data.get('username', ''), password)
        stored_password = '' if secret_arn else password

        cursor.execute('''
            INSERT INTO sources (name, engine, host, port, database_name, schema_name,
                                 username, password, password_secret_arn, ssl_mode, status, created, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('engine', 'postgres'), data.get('host'),
            port, data.get('database_name'), data.get('schema_name', 'public'),
            data.get('username'), stored_password, secret_arn or '',
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
        conn = get_db()
        conn.execute('''
            UPDATE sources SET name=?, engine=?, host=?, port=?, database_name=?,
                schema_name=?, username=?, password=COALESCE(NULLIF(?, ''), password),
                ssl_mode=?, status=?, notes=?
            WHERE id=?
        ''', (
            data.get('name'), data.get('engine'), data.get('host'), data.get('port'),
            data.get('database_name'), data.get('schema_name'), data.get('username'),
            data.get('password'), data.get('ssl_mode'), data.get('status'), data.get('notes'), id
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

        if engine == 'postgres':
            import psycopg2
            pg_conn = psycopg2.connect(
                host=s['host'], port=s.get('port', 5432),
                dbname=s['database_name'], user=s.get('username'),
                password=s.get('password'),
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
                database=s['database_name'], user=s.get('username'),
                password=s.get('password'),
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
# SINK TEMPLATES
# ============================================================

@app.route('/api/sink-templates', methods=['GET'])
def get_sink_templates():
    """List available sink worker templates from templates/sink/ folder."""
    try:
        templates_dir = os.path.join(os.path.dirname(__file__), 'templates', 'sink')
        templates = []
        if os.path.isdir(templates_dir):
            for filename in sorted(os.listdir(templates_dir)):
                if filename.endswith('.py'):
                    filepath = os.path.join(templates_dir, filename)
                    template_name = filename[:-3]  # strip .py
                    display_name = template_name
                    # Read first line for # Template: <name>
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            first_line = f.readline().strip()
                            if first_line.startswith('# Template:'):
                                display_name = first_line.split(':', 1)[1].strip()
                    except Exception:
                        pass
                    templates.append({'id': template_name, 'name': display_name})
        return jsonify(templates)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
# SMART PIPELINE BUILDER (Agentic AI)
# ============================================================

import traceback

# Agent tool definitions for Bedrock
AGENT_TOOLS = [
    {
        "name": "list_sources",
        "description": "List all configured data sources in ECDL. Returns id, name, engine, host, port, database_name, schema_name, status for each source.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_brokers",
        "description": "List all configured message brokers. Returns id, name, broker_type, bootstrap_servers, auth_type, region, status for each broker.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_targets",
        "description": "List all configured Valkey targets. Returns id, name, cluster_endpoint, port, write_mode, sink_template, status for each target.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_pipelines",
        "description": "List all existing pipelines with their linked source, connector, broker, and target names.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_sink_templates",
        "description": "List available sink worker templates (e.g. valkey_default, valkey_glide_cluster).",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "scan_tables",
        "description": "Connect to a data source and discover all available tables. Use this to help the user pick which tables to replicate.",
        "input_schema": {
            "type": "object",
            "properties": {"source_id": {"type": "integer", "description": "ID of the data source to scan"}},
            "required": ["source_id"]
        }
    },
    {
        "name": "test_db_connection",
        "description": "Test connectivity to a database. Use before creating a source to validate the connection works.",
        "input_schema": {
            "type": "object",
            "properties": {
                "engine": {"type": "string", "enum": ["postgres", "mysql"]},
                "host": {"type": "string"}, "port": {"type": "integer"},
                "database_name": {"type": "string"}, "username": {"type": "string"},
                "password": {"type": "string"}, "ssl_mode": {"type": "string"}
            },
            "required": ["engine", "host", "port", "database_name", "username", "password"]
        }
    },
    {
        "name": "create_source",
        "description": "Create a new data source in ECDL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "engine": {"type": "string", "enum": ["postgres", "mysql", "oracle", "sqlserver"]},
                "host": {"type": "string"}, "port": {"type": "integer"},
                "database_name": {"type": "string"}, "schema_name": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"},
                "ssl_mode": {"type": "string"}
            },
            "required": ["name", "engine", "host", "port", "database_name", "username", "password"]
        }
    },
    {
        "name": "create_broker",
        "description": "Create a new message broker in ECDL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "broker_type": {"type": "string", "enum": ["kafka", "kinesis"]},
                "bootstrap_servers": {"type": "string"}, "auth_type": {"type": "string", "enum": ["none", "iam"]},
                "region": {"type": "string"}, "partitions": {"type": "integer"}, "retention_hours": {"type": "integer"}
            },
            "required": ["name", "broker_type"]
        }
    },
    {
        "name": "create_target",
        "description": "Create a new Valkey target in ECDL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "cluster_endpoint": {"type": "string"},
                "port": {"type": "integer"}, "auth_token": {"type": "string"},
                "ssl_enabled": {"type": "integer"}, "cluster_mode": {"type": "integer"},
                "key_prefix": {"type": "string"}, "key_pattern": {"type": "string"},
                "write_mode": {"type": "string", "enum": ["hset", "json", "string"]},
                "pipeline_batch_size": {"type": "integer"}, "lww_enabled": {"type": "integer"},
                "sink_template": {"type": "string"}
            },
            "required": ["name", "cluster_endpoint"]
        }
    },
    {
        "name": "create_pipeline",
        "description": "Create a complete pipeline by linking a source, connector, broker, and target. This also auto-creates the Debezium connector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
                "source_id": {"type": "integer"}, "broker_id": {"type": "integer"}, "target_id": {"type": "integer"},
                "tables": {"type": "string", "description": "Comma-separated table names to replicate"},
                "connector_type": {"type": "string"}, "snapshot_mode": {"type": "string"},
                "slot_name": {"type": "string"}, "publication_name": {"type": "string"}
            },
            "required": ["pipeline_name", "source_id", "broker_id", "target_id", "tables"]
        }
    }
]


def _execute_agent_tool(tool_name, tool_input):
    """Execute an agent tool and return the result."""
    conn = get_db()
    now = datetime.now().strftime('%Y-%m-%d')
    try:
        if tool_name == 'list_sources':
            rows = conn.execute('SELECT id, name, engine, host, port, database_name, schema_name, status FROM sources ORDER BY name').fetchall()
            return json.dumps([dict(r) for r in rows])

        elif tool_name == 'list_brokers':
            rows = conn.execute('SELECT id, name, broker_type, bootstrap_servers, auth_type, region, status FROM brokers ORDER BY name').fetchall()
            return json.dumps([dict(r) for r in rows])

        elif tool_name == 'list_targets':
            rows = conn.execute('SELECT id, name, cluster_endpoint, port, write_mode, sink_template, status FROM targets ORDER BY name').fetchall()
            return json.dumps([dict(r) for r in rows])

        elif tool_name == 'list_pipelines':
            rows = conn.execute('''
                SELECT p.id, p.name, p.status, s.name as source_name, c.name as connector_name,
                       b.name as broker_name, t.name as target_name
                FROM pipelines p
                LEFT JOIN sources s ON p.source_id = s.id LEFT JOIN connectors c ON p.connector_id = c.id
                LEFT JOIN brokers b ON p.broker_id = b.id LEFT JOIN targets t ON p.target_id = t.id
                ORDER BY p.name
            ''').fetchall()
            return json.dumps([dict(r) for r in rows])

        elif tool_name == 'list_sink_templates':
            templates_dir = os.path.join(os.path.dirname(__file__), 'templates', 'sink')
            templates = []
            if os.path.isdir(templates_dir):
                for fn in sorted(os.listdir(templates_dir)):
                    if fn.endswith('.py'):
                        tid = fn[:-3]
                        dname = tid
                        with open(os.path.join(templates_dir, fn), 'r') as f:
                            fl = f.readline().strip()
                            if fl.startswith('# Template:'): dname = fl.split(':', 1)[1].strip()
                        templates.append({'id': tid, 'name': dname})
            return json.dumps(templates)

        elif tool_name == 'scan_tables':
            source = conn.execute('SELECT * FROM sources WHERE id=?', (tool_input['source_id'],)).fetchone()
            if not source: return json.dumps({"error": "Source not found"})
            s = dict(source)
            if s.get('engine') == 'postgres':
                import psycopg2
                pg = psycopg2.connect(host=s['host'], port=s.get('port', 5432), dbname=s['database_name'],
                                      user=s.get('username'), password=s.get('password'),
                                      sslmode=s.get('ssl_mode', 'require'), connect_timeout=10)
                cur = pg.cursor()
                cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')
                    ORDER BY table_name""")
                tables = [r[0] for r in cur.fetchall()]
                cur.close(); pg.close()
                return json.dumps({"tables": tables, "count": len(tables)})
            elif s.get('engine') == 'mysql':
                import pymysql
                my = pymysql.connect(host=s['host'], port=s.get('port', 3306), database=s['database_name'],
                                     user=s.get('username'), password=s.get('password'), connect_timeout=10)
                cur = my.cursor()
                cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name", (s['database_name'],))
                tables = [r[0] for r in cur.fetchall()]
                cur.close(); my.close()
                return json.dumps({"tables": tables, "count": len(tables)})
            return json.dumps({"error": f"Unsupported engine: {s.get('engine')}"})

        elif tool_name == 'test_db_connection':
            ti = tool_input
            try:
                if ti['engine'] == 'postgres':
                    import psycopg2
                    pg = psycopg2.connect(host=ti['host'], port=ti['port'], dbname=ti['database_name'],
                                          user=ti['username'], password=ti['password'],
                                          sslmode=ti.get('ssl_mode', 'require'), connect_timeout=10)
                    cur = pg.cursor(); cur.execute("SELECT version()"); ver = cur.fetchone()[0]
                    cur.close(); pg.close()
                    return json.dumps({"success": True, "version": ver})
                elif ti['engine'] == 'mysql':
                    import pymysql
                    my = pymysql.connect(host=ti['host'], port=ti['port'], database=ti['database_name'],
                                         user=ti['username'], password=ti['password'], connect_timeout=10)
                    cur = my.cursor(); cur.execute("SELECT version()"); ver = cur.fetchone()[0]
                    cur.close(); my.close()
                    return json.dumps({"success": True, "version": ver})
                return json.dumps({"error": f"Unsupported engine: {ti['engine']}"})
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)})

        elif tool_name == 'create_source':
            ti = tool_input
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO sources (name, engine, host, port, database_name, schema_name,
                username, password, ssl_mode, status, created, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ti['name'], ti.get('engine', 'postgres'), ti['host'], ti.get('port', 5432),
                 ti['database_name'], ti.get('schema_name', 'public'), ti['username'], ti['password'],
                 ti.get('ssl_mode', 'require'), 'configured', now, 'Created by AI Agent'))
            conn.commit()
            return json.dumps({"success": True, "source_id": cursor.lastrowid})

        elif tool_name == 'create_broker':
            ti = tool_input
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO brokers (name, broker_type, bootstrap_servers, auth_type,
                stream_name, region, partitions, retention_hours, config_json, status, created, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ti['name'], ti.get('broker_type', 'kafka'), ti.get('bootstrap_servers'),
                 ti.get('auth_type', 'none'), ti.get('stream_name'), ti.get('region', 'us-east-1'),
                 ti.get('partitions', 3), ti.get('retention_hours', 168), None, 'configured', now, 'Created by AI Agent'))
            conn.commit()
            return json.dumps({"success": True, "broker_id": cursor.lastrowid})

        elif tool_name == 'create_target':
            ti = tool_input
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO targets (name, cluster_endpoint, port, auth_token, ssl_enabled,
                cluster_mode, key_prefix, key_pattern, write_mode, pipeline_batch_size, lww_enabled,
                sink_template, config_json, status, created, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ti['name'], ti['cluster_endpoint'], ti.get('port', 6379), ti.get('auth_token'),
                 ti.get('ssl_enabled', 1), ti.get('cluster_mode', 1), ti.get('key_prefix', ''),
                 ti.get('key_pattern', '{table}:{pk}'), ti.get('write_mode', 'hset'),
                 ti.get('pipeline_batch_size', 100), ti.get('lww_enabled', 1),
                 ti.get('sink_template', 'valkey_default'), None, 'configured', now, 'Created by AI Agent'))
            conn.commit()
            return json.dumps({"success": True, "target_id": cursor.lastrowid})

        elif tool_name == 'create_pipeline':
            ti = tool_input
            cursor = conn.cursor()
            pname = ti['pipeline_name']
            safe = pname.lower().replace(' ', '_').replace('-', '_')
            src = conn.execute('SELECT engine FROM sources WHERE id=?', (ti['source_id'],)).fetchone()
            engine = dict(src)['engine'] if src else 'postgres'
            ctype = ti.get('connector_type', f'debezium-{engine}')
            cursor.execute('''INSERT INTO connectors (name, source_id, connector_type, tables, snapshot_mode,
                slot_name, publication_name, transforms, config_json, status, created)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (f"{pname}-connector", ti['source_id'], ctype, ti['tables'],
                 ti.get('snapshot_mode', 'initial'), ti.get('slot_name', f'debezium_{safe}'),
                 ti.get('publication_name', f'pub_{safe}'), None, None, 'configured', now))
            connector_id = cursor.lastrowid
            cursor.execute('''INSERT INTO pipelines (name, source_id, connector_id, broker_id, target_id,
                status, created, notes) VALUES (?,?,?,?,?,?,?,?)''',
                (pname, ti['source_id'], connector_id, ti['broker_id'], ti['target_id'],
                 'draft', now, 'Created by AI Agent'))
            conn.commit()
            return json.dumps({"success": True, "pipeline_id": cursor.lastrowid, "connector_id": connector_id})

        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


AGENT_SYSTEM_PROMPT = """You are the ECDL Smart Pipeline Builder — an AI agent that helps users create CDC (Change Data Capture) pipelines from RDBMS databases to Amazon ElastiCache for Valkey.

You have access to tools that let you inspect existing ECDL resources, scan database tables, test connections, and create new sources, brokers, targets, and pipelines.

Your workflow:
1. Start by understanding what the user wants to replicate and where.
2. Check existing resources (sources, brokers, targets) — reuse them when possible instead of creating duplicates.
3. If the user provides database connection details, test the connection first, then scan tables.
4. Recommend optimal settings based on what you discover (snapshot mode, slot naming, key patterns, sink templates).
5. When you have enough information, create all the resources and assemble the pipeline.
6. After building, summarize what was created.

Key knowledge:
- Debezium snapshot modes: 'initial' (snapshot existing + stream changes), 'never' (CDC only), 'initial_only' (one-time snapshot), 'when_needed' (auto-detect)
- Tables without primary keys will map to a single Valkey key — warn the user about this.
- For MSK with IAM auth, the broker auth_type should be 'iam' and the deployment needs proper IAM policies.
- Sink templates: 'valkey_default' for standalone/cluster using valkey-py, 'valkey_glide_cluster' for Glide cluster client.
- Each pipeline needs a unique replication slot name and publication name to avoid conflicts.
- Key pattern '{table}:{pk}' is the default — recommend it unless the user has specific needs.

Be concise, helpful, and proactive. Ask clarifying questions only when essential information is missing. When you have enough context, proceed autonomously."""


@app.route('/api/smart-builder/chat', methods=['POST'])
def smart_builder_chat():
    """Agentic AI chat endpoint. Runs an agent loop with Bedrock Claude."""
    try:
        data = request.json
        messages = data.get('messages', [])

        if not messages:
            return jsonify({'error': 'No messages provided'}), 400

        import boto3
        bedrock = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_DEFAULT_REGION', 'us-east-1'))

        # Agent loop — up to 10 tool-use iterations
        bedrock_messages = []
        for m in messages:
            bedrock_messages.append({"role": m['role'], "content": [{"text": m['content']}]})

        bedrock_tools = [{"toolSpec": {"name": t["name"], "description": t["description"],
                          "inputSchema": {"json": t["input_schema"]}}} for t in AGENT_TOOLS]

        for iteration in range(10):
            response = bedrock.converse(
                modelId='us.anthropic.claude-3-7-sonnet-20250219-v1:0',
                system=[{"text": AGENT_SYSTEM_PROMPT}],
                messages=bedrock_messages,
                toolConfig={"tools": bedrock_tools},
                inferenceConfig={"maxTokens": 4096, "temperature": 0.1}
            )

            output = response['output']['message']
            bedrock_messages.append(output)

            # Check if the model wants to use tools
            tool_uses = [b for b in output['content'] if 'toolUse' in b]
            if not tool_uses:
                # No tool use — extract text response and return
                text_parts = [b['text'] for b in output['content'] if 'text' in b]
                assistant_text = '\n'.join(text_parts)
                return jsonify({'response': assistant_text, 'done': True})

            # Execute each tool call
            tool_results = []
            for block in output['content']:
                if 'toolUse' in block:
                    tu = block['toolUse']
                    tool_name = tu['name']
                    tool_input = tu.get('input', {})
                    tool_id = tu['toolUseId']
                    print(f"  Agent tool call: {tool_name}({json.dumps(tool_input)[:200]})")
                    result = _execute_agent_tool(tool_name, tool_input)
                    tool_results.append({
                        "toolResultId": tool_id,
                        "content": [{"text": result}]
                    })

            # Feed tool results back
            bedrock_messages.append({
                "role": "user",
                "content": [{"toolResult": {"toolUseId": tr["toolResultId"], "content": tr["content"]}} for tr in tool_results]
            })

        # If we exhausted iterations, return what we have
        return jsonify({'response': 'The request reached the processing limit. Please try again with a simpler request.', 'done': True})

    except Exception as e:
        print(f"Smart builder chat error: {traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500


# ============================================================
# GENERATE CONFIGS (Debezium JSON, docker-compose, etc.)
# ============================================================

@app.route('/api/pipelines/<int:id>/generate-config', methods=['GET'])
def generate_pipeline_config(id):
    """Generate Debezium connector config + sink worker config for a pipeline."""
    try:
        conn = get_db()
        pipeline = conn.execute('''
            SELECT p.*,
                   s.engine, s.host, s.port as src_port, s.database_name,
                   s.schema_name, s.username, s.password,
                   c.connector_type, c.tables, c.snapshot_mode, c.slot_name, c.publication_name,
                   b.broker_type, b.bootstrap_servers, b.auth_type, b.stream_name, b.region as broker_region,
                   b.partitions,
                   t.cluster_endpoint, t.port as tgt_port, t.auth_token,
                   t.key_prefix, t.key_pattern, t.write_mode,
                   t.pipeline_batch_size, t.lww_enabled, t.ssl_enabled as tgt_ssl
            FROM pipelines p
            LEFT JOIN sources s ON p.source_id = s.id
            LEFT JOIN connectors c ON p.connector_id = c.id
            LEFT JOIN brokers b ON p.broker_id = b.id
            LEFT JOIN targets t ON p.target_id = t.id
            WHERE p.id = ?
        ''', (id,)).fetchone()
        conn.close()

        if not pipeline:
            return jsonify({'error': 'Pipeline not found'}), 404

        p = dict(pipeline)
        topic_name = p['name'].replace(' ', '_')

        # Build Debezium connector config
        db_engine = p.get('engine', 'postgres')
        debezium_config = {
            "name": f"ecdl-{p['name']}-connector",
            "config": {
                "connector.class": _debezium_class(db_engine),
                "database.hostname": p.get('host', ''),
                "database.port": str(p.get('src_port', 5432)),
                "database.user": p.get('username', ''),
                "database.password": p.get('password', ''),
                "database.dbname": p.get('database_name', ''),
                "database.server.name": p['name'],
                "table.include.list": _qualify_tables(p.get('tables', ''), p.get('schema_name', 'public'), db_engine, p.get('database_name', '')),
                "snapshot.mode": p.get('snapshot_mode', 'initial'),
                "topic.prefix": f"ecdl.{topic_name}",
                "plugin.name": "pgoutput" if db_engine == 'postgres' else "decoderbufs",
            }
        }

        if p.get('slot_name'):
            debezium_config['config']['slot.name'] = p['slot_name']
        if p.get('publication_name'):
            debezium_config['config']['publication.name'] = p['publication_name']

        # JSON converters
        debezium_config['config']['key.converter'] = 'org.apache.kafka.connect.json.JsonConverter'
        debezium_config['config']['value.converter'] = 'org.apache.kafka.connect.json.JsonConverter'
        debezium_config['config']['key.converter.schemas.enable'] = 'false'
        debezium_config['config']['value.converter.schemas.enable'] = 'false'

        # Topic creation (Debezium-managed)
        debezium_config['config']['topic.creation.enable'] = 'true'
        debezium_config['config']['topic.creation.default.replication.factor'] = str(min(p.get('partitions', 3), 3))
        debezium_config['config']['topic.creation.default.partitions'] = str(p.get('partitions', 3))

        # Broker output config
        if p.get('broker_type') == 'kinesis':
            debezium_config['config']['database.history.kinesis.stream.name'] = p.get('stream_name', '')
            debezium_config['config']['database.history.kinesis.region'] = p.get('broker_region', 'us-east-1')

        # Sink worker config
        sink_config = {
            "valkey_endpoint": p.get('cluster_endpoint', ''),
            "valkey_port": p.get('tgt_port', 6379),
            "valkey_ssl": bool(p.get('tgt_ssl', True)),
            "key_prefix": p.get('key_prefix', ''),
            "key_pattern": p.get('key_pattern', '{table}:{pk}'),
            "write_mode": p.get('write_mode', 'hset'),
            "pipeline_batch_size": p.get('pipeline_batch_size', 100),
            "lww_enabled": bool(p.get('lww_enabled', True)),
            "broker_type": p.get('broker_type', 'kafka'),
            "bootstrap_servers": p.get('bootstrap_servers', ''),
            "stream_name": p.get('stream_name', ''),
            "consumer_group": f"ecdl-sink-{topic_name}"
        }

        return jsonify({
            'success': True,
            'debezium_config': debezium_config,
            'sink_config': sink_config
        })
    except Exception as e:
        print(f"Error generating config: {e}")
        return jsonify({'error': str(e)}), 500


def _debezium_class(engine):
    mapping = {
        'postgres': 'io.debezium.connector.postgresql.PostgresConnector',
        'mysql': 'io.debezium.connector.mysql.MySqlConnector',
        'oracle': 'io.debezium.connector.oracle.OracleConnector',
        'sqlserver': 'io.debezium.connector.sqlserver.SqlServerConnector',
    }
    return mapping.get(engine, mapping['postgres'])


def _debezium_image(engine):
    mapping = {
        'postgres': 'debezium/connect:2.5',
        'mysql': 'debezium/connect:2.5',
        'oracle': 'debezium/connect:2.5',
        'sqlserver': 'debezium/connect:2.5',
    }
    return mapping.get(engine, mapping['postgres'])


def _qualify_tables(tables_str, schema='public', engine='postgres', database_name=''):
    """Ensure table names are schema-qualified for Debezium.
    PostgreSQL: public.table_name
    MySQL: database_name.table_name
    """
    if not tables_str:
        return ''
    # For MySQL, use database name as the qualifier
    if engine == 'mysql':
        qualifier = database_name or schema
    else:
        qualifier = schema or 'public'

    qualified = []
    for t in tables_str.split(','):
        t = t.strip()
        if not t:
            continue
        # Already qualified if it contains a dot
        if '.' in t:
            qualified.append(t)
        else:
            qualified.append(f"{qualifier}.{t}")
    return ','.join(qualified)


# ---------------------------------------------------------------------------
# Shared parts of the two legacy Kafka packages (Docker Compose and ECS
# Fargate). Both generators call these, so the two packages cannot drift.
# ---------------------------------------------------------------------------

_JSON_CONVERTER = 'org.apache.kafka.connect.json.JsonConverter'

SINK_DOCKERFILE = '''FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sink_common.py sink_worker.py ./

CMD ["python", "-u", "sink_worker.py"]
'''


def _msk_iam_client_settings(prefix=''):
    """The four Kafka client settings for MSK IAM auth, under a key prefix."""
    return {
        prefix + 'security.protocol': 'SASL_SSL',
        prefix + 'sasl.mechanism': 'AWS_MSK_IAM',
        prefix + 'sasl.jaas.config':
            'software.amazon.msk.auth.iam.IAMLoginModule required;',
        prefix + 'sasl.client.callback.handler.class':
            'software.amazon.msk.auth.iam.IAMClientCallbackHandler',
    }


def _connect_worker_env(kafka_bootstrap, pipeline_name):
    """Environment for a stock Debezium Connect image (no MSK IAM)."""
    return {
        'BOOTSTRAP_SERVERS': kafka_bootstrap,
        'GROUP_ID': f'ecdl-{pipeline_name}-connect',
        'CONFIG_STORAGE_TOPIC': f'ecdl_{pipeline_name}_connect_configs',
        'OFFSET_STORAGE_TOPIC': f'ecdl_{pipeline_name}_connect_offsets',
        'STATUS_STORAGE_TOPIC': f'ecdl_{pipeline_name}_connect_status',
        'KEY_CONVERTER': _JSON_CONVERTER,
        'VALUE_CONVERTER': _JSON_CONVERTER,
        'CONNECT_KEY_CONVERTER_SCHEMAS_ENABLE': 'false',
        'CONNECT_VALUE_CONVERTER_SCHEMAS_ENABLE': 'false',
        'CONNECT_TOPIC_CREATION_ENABLE': 'true',
    }


def _connect_worker_iam_env():
    """The MSK IAM settings for the Connect worker and its clients, as the
    CONNECT_* environment variables the stock image turns into properties."""
    env = {}
    for prefix in ('', 'producer.', 'consumer.'):
        for key, value in _msk_iam_client_settings(prefix).items():
            env['CONNECT_' + key.upper().replace('.', '_')] = value
    return env


def _debezium_connector(p, pipeline_name, topic_name, db_engine, db_host):
    """The Debezium connector registration both packages submit."""
    config = {
        "connector.class": _debezium_class(db_engine),
        "database.hostname": db_host,
        "database.port": str(p.get('src_port', 5432)),
        "database.user": p.get('username', ''),
        "database.password": p.get('password', ''),
        "database.dbname": p.get('database_name', ''),
        "database.server.name": p['name'],
        "table.include.list": _qualify_tables(
            p.get('tables', ''), p.get('schema_name', 'public'), db_engine,
            p.get('database_name', '')),
        "snapshot.mode": p.get('snapshot_mode', 'initial'),
        "topic.prefix": f"ecdl.{topic_name}",
        "plugin.name": "pgoutput" if db_engine == 'postgres' else "decoderbufs",
        "key.converter": _JSON_CONVERTER,
        "value.converter": _JSON_CONVERTER,
        "key.converter.schemas.enable": "false",
        "value.converter.schemas.enable": "false",
    }
    if p.get('slot_name'):
        config['slot.name'] = p['slot_name']
    if p.get('publication_name'):
        config['publication.name'] = p['publication_name']
    # Topic auto-creation (needed for MSK, which may not auto-create topics).
    config['topic.creation.enable'] = 'true'
    config['topic.creation.default.replication.factor'] = str(
        min(p.get('partitions', 3), 3))
    config['topic.creation.default.partitions'] = str(p.get('partitions', 3))
    return {"name": f"ecdl-{pipeline_name}-connector", "config": config}


def _sink_requirements(sink_template_name, is_msk_iam):
    """requirements.txt for the generated Python sink worker."""
    if 'glide' in sink_template_name:
        text = 'kafka-python>=2.0.2\nvalkey-glide>=1.0.0\n'
    else:
        text = 'kafka-python>=2.0.2\nvalkey>=5.0.0\n'
    text += 'boto3>=1.28.0\n'
    if is_msk_iam:
        text += 'aws-msk-iam-sasl-signer-python>=1.0.0\n'
    return text


def _read_sink_common():
    """sink_common.py, the code every sink template imports."""
    path = os.path.join(os.path.dirname(__file__), 'templates', 'sink_common.py')
    with open(path, 'r') as f:
        return f.read()


def _read_sink_template(sink_template_name):
    """Source of the named sink template, falling back to valkey_default."""
    templates_dir = os.path.join(os.path.dirname(__file__), 'templates', 'sink')
    template_path = os.path.join(templates_dir, f'{sink_template_name}.py')
    if not os.path.isfile(template_path):
        template_path = os.path.join(templates_dir, 'valkey_default.py')
    with open(template_path, 'r') as f:
        return f.read()


def _msk_connect_properties(kafka_bootstrap, pipeline_name):
    """connect-distributed.properties with MSK IAM auth set for the worker and
    every client it creates."""
    lines = [
        '# Auto-generated Kafka Connect distributed config for MSK IAM',
        f'bootstrap.servers={kafka_bootstrap}',
        f'group.id=ecdl-{pipeline_name}-connect',
        '',
        '# Storage topics — replication factor MUST be 3 for MSK Serverless',
        f'config.storage.topic=ecdl_{pipeline_name}_connect_configs',
        f'offset.storage.topic=ecdl_{pipeline_name}_connect_offsets',
        f'status.storage.topic=ecdl_{pipeline_name}_connect_status',
        'config.storage.replication.factor=3',
        'offset.storage.replication.factor=3',
        'status.storage.replication.factor=3',
        'offset.flush.interval.ms=10000',
        '',
        '# Converters',
        f'key.converter={_JSON_CONVERTER}',
        f'value.converter={_JSON_CONVERTER}',
        'key.converter.schemas.enable=false',
        'value.converter.schemas.enable=false',
        '',
        '# REST API',
        'listeners=HTTP://0.0.0.0:8083',
        'plugin.path=/kafka/connect',
        '',
        '# Connector client config override policy',
        'connector.client.config.override.policy=All',
        '',
        '# Enable topic creation by connectors',
        'topic.creation.enable=true',
    ]
    for prefix, label in (('', 'the Connect worker itself'),
                          ('producer.', 'producer clients inside Connect'),
                          ('consumer.', 'consumer clients inside Connect'),
                          ('admin.', 'admin client')):
        lines += ['', f'# ---- SASL/IAM for {label} ----']
        lines += [f'{k}={v}' for k, v in _msk_iam_client_settings(prefix).items()]
    return '\n'.join(lines) + '\n'


def _msk_debezium_dockerfile(base_image):
    """Debezium Connect image with the MSK IAM auth library and the properties
    file above included."""
    return f'''FROM {base_image}

# Download AWS MSK IAM Auth library for SASL/IAM authentication
USER root
RUN cd /kafka/libs && \\
    curl -sL -o aws-msk-iam-auth-2.2.0-all.jar \\
    https://github.com/aws/aws-msk-iam-auth/releases/download/v2.2.0/aws-msk-iam-auth-2.2.0-all.jar

# Copy custom connect-distributed.properties with SASL/IAM settings
COPY connect-distributed.properties /kafka/config/connect-distributed.properties
RUN chmod 644 /kafka/config/connect-distributed.properties && \\
    chown kafka:kafka /kafka/config/connect-distributed.properties

# Symlink log4j config so connect-distributed.sh can find it at the standard Kafka path
RUN ln -sf /kafka/config/log4j.properties /kafka/config/connect-log4j.properties

USER kafka

# Override ENTRYPOINT to bypass Debezium's docker-entrypoint.sh which ignores SASL env vars
ENTRYPOINT ["/kafka/bin/connect-distributed.sh"]
CMD ["/kafka/config/connect-distributed.properties"]
'''


def _add_msk_debezium_files(files, db_engine, kafka_bootstrap, pipeline_name):
    """Add the custom Debezium image files an MSK IAM package needs."""
    files['debezium-connect/connect-distributed.properties'] = \
        _msk_connect_properties(kafka_bootstrap, pipeline_name)
    files['debezium-connect/Dockerfile'] = \
        _msk_debezium_dockerfile(_debezium_image(db_engine))


@app.route('/api/pipelines/<int:id>/generate-docker-compose', methods=['GET'])
def generate_docker_compose(id):
    """Generate a complete Docker Compose deployment package for a pipeline."""
    try:
        conn = get_db()
        pipeline = conn.execute('''
            SELECT p.*,
                   s.name as source_name, s.engine, s.host, s.port as src_port, s.database_name,
                   s.schema_name, s.username, s.password,
                   c.name as connector_name, c.connector_type, c.tables, c.snapshot_mode,
                   c.slot_name, c.publication_name, c.transforms,
                   b.name as broker_name, b.broker_type, b.bootstrap_servers, b.auth_type,
                   b.stream_name,
                   b.region as broker_region, b.partitions,
                   t.name as target_name, t.cluster_endpoint, t.port as tgt_port, t.auth_token,
                   t.key_prefix, t.key_pattern, t.write_mode,
                   t.pipeline_batch_size, t.lww_enabled, t.ssl_enabled as tgt_ssl,
                   t.sink_template, t.dev_test, t.sink_replicas
            FROM pipelines p
            LEFT JOIN sources s ON p.source_id = s.id
            LEFT JOIN connectors c ON p.connector_id = c.id
            LEFT JOIN brokers b ON p.broker_id = b.id
            LEFT JOIN targets t ON p.target_id = t.id
            WHERE p.id = ?
        ''', (id,)).fetchone()
        conn.close()

        if not pipeline:
            return jsonify({'error': 'Pipeline not found'}), 404

        p = dict(pipeline)
        pipeline_name = p['name'].lower().replace(' ', '-')
        topic_name = p['name'].replace(' ', '_')
        db_engine = p.get('engine', 'postgres')

        # Replace localhost with host.docker.internal for Docker networking.
        # These are hostnames a user typed for their source/target, compared to
        # mean "this machine"; nothing binds to them here.
        local_names = ('localhost', '127.0.0.1', '0.0.0.0')  # nosec B104 - compared, not bound
        db_host = p.get('host', '')
        if db_host in local_names:
            db_host = 'host.docker.internal'
        valkey_host = p.get('cluster_endpoint', 'localhost')
        if valkey_host in local_names:
            valkey_host = 'host.docker.internal'
        needs_host_gateway = db_host == 'host.docker.internal' or valkey_host == 'host.docker.internal'

        # Determine if using external Kafka (MSK, Confluent, etc.) or local
        bootstrap_servers = (p.get('bootstrap_servers') or '').strip()
        use_local_kafka = not bootstrap_servers or bootstrap_servers in ('localhost:9092', '127.0.0.1:9092')

        # Build Docker Compose structure
        compose = {
            'version': '3.8',
            'services': {},
            'networks': {
                'ecdl-network': {'driver': 'bridge'}
            }
        }

        if use_local_kafka:
            # Local Kafka mode — include Zookeeper + Kafka containers
            compose['volumes'] = {
                'kafka-data': {},
                'zookeeper-data': {}
            }
            kafka_bootstrap = 'kafka:29092'

            compose['services']['zookeeper'] = {
                'image': 'confluentinc/cp-zookeeper:7.5.0',
                'container_name': f'ecdl-{pipeline_name}-zookeeper',
                'environment': {
                    'ZOOKEEPER_CLIENT_PORT': 2181,
                    'ZOOKEEPER_TICK_TIME': 2000
                },
                'volumes': ['zookeeper-data:/var/lib/zookeeper/data'],
                'networks': ['ecdl-network'],
                'healthcheck': {
                    'test': ['CMD', 'nc', '-z', 'localhost', '2181'],
                    'interval': '10s',
                    'timeout': '5s',
                    'retries': 5
                }
            }

            compose['services']['kafka'] = {
                'image': 'confluentinc/cp-kafka:7.5.0',
                'container_name': f'ecdl-{pipeline_name}-kafka',
                'depends_on': {'zookeeper': {'condition': 'service_healthy'}},
                # Loopback publish: a bare '9092:9092' exposes an unauthenticated
                # PLAINTEXT broker on every interface of the host.
                'ports': ['127.0.0.1:9092:9092'],
                'environment': {
                    'KAFKA_BROKER_ID': 1,
                    'KAFKA_ZOOKEEPER_CONNECT': 'zookeeper:2181',
                    'KAFKA_ADVERTISED_LISTENERS': 'PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092',
                    'KAFKA_LISTENER_SECURITY_PROTOCOL_MAP': 'PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT',
                    'KAFKA_INTER_BROKER_LISTENER_NAME': 'PLAINTEXT',
                    'KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR': 1,
                    'KAFKA_TRANSACTION_STATE_LOG_MIN_ISR': 1,
                    'KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR': 1,
                    'KAFKA_AUTO_CREATE_TOPICS_ENABLE': 'true'
                },
                'volumes': ['kafka-data:/var/lib/kafka/data'],
                'networks': ['ecdl-network'],
                'healthcheck': {
                    'test': ['CMD-SHELL', 'kafka-broker-api-versions --bootstrap-server kafka:29092 > /dev/null 2>&1'],
                    'interval': '10s',
                    'timeout': '10s',
                    'retries': 10,
                    'start_period': '30s'
                }
            }
        else:
            # External Kafka mode (MSK, Confluent Cloud, etc.)
            kafka_bootstrap = bootstrap_servers

        # Determine auth type for external Kafka
        auth_type = (p.get('auth_type') or 'none').strip()
        is_msk_iam = auth_type == 'iam'

        # Debezium Connect service
        debezium_env = {}

        # MSK IAM authentication for Debezium Connect
        if is_msk_iam:
            broker_region = p.get('broker_region', 'us-east-1')
            # Only AWS region env vars needed — all Kafka config is in connect-distributed.properties
            debezium_env.update({
                'AWS_REGION': broker_region,
                'AWS_DEFAULT_REGION': broker_region,
            })
        else:
            # Non-IAM: use Debezium's standard env var convention (processed by docker-entrypoint.sh)
            debezium_env.update(_connect_worker_env(kafka_bootstrap, pipeline_name))

        debezium_service = {
            'container_name': f'ecdl-{pipeline_name}-debezium',
            # Loopback publish: the Connect REST API has no auth and returns the
            # connector config, database password included. start.sh only ever
            # calls it on localhost.
            'ports': ['127.0.0.1:8083:8083'],
            'environment': debezium_env,
            'networks': ['ecdl-network'],
            'healthcheck': {
                'test': ['CMD-SHELL', 'curl -sf http://localhost:8083/ || exit 1'],
                'interval': '10s',
                'timeout': '10s',
                'retries': 12,
                'start_period': '60s'
            }
        }
        if is_msk_iam:
            debezium_service['build'] = {
                'context': './debezium-connect',
                'dockerfile': 'Dockerfile'
            }
            # Use host network so container can reach EC2 IMDS for instance profile credentials
            debezium_service['network_mode'] = 'host'
            del debezium_service['networks']
            del debezium_service['ports']
        else:
            debezium_service['image'] = _debezium_image(db_engine)
        if use_local_kafka:
            debezium_service['depends_on'] = {'kafka': {'condition': 'service_healthy'}}
        if needs_host_gateway:
            debezium_service['extra_hosts'] = ['host.docker.internal:host-gateway']
        compose['services']['debezium-connect'] = debezium_service

        # Valkey Sink Worker service
        sink_service = {
            'build': {
                'context': './sink-worker',
                'dockerfile': 'Dockerfile'
            },
            'container_name': f'ecdl-{pipeline_name}-sink',
            'environment': {
                'KAFKA_BOOTSTRAP_SERVERS': kafka_bootstrap,
                'KAFKA_CONSUMER_GROUP': f'ecdl-sink-{pipeline_name}-{int(time.time())}' if p.get('dev_test') else f'ecdl-sink-{pipeline_name}',
                'KAFKA_TOPIC_PATTERN': f'ecdl.{topic_name}.*',
                'VALKEY_HOST': valkey_host,
                'VALKEY_PORT': str(p.get('tgt_port', 6379)),
                'VALKEY_SSL': str(bool(p.get('tgt_ssl', True))).lower(),
                'VALKEY_AUTH_TOKEN': p.get('auth_token', ''),
                'KEY_PREFIX': p.get('key_prefix', ''),
                'KEY_PATTERN': p.get('key_pattern', '{table}:{pk}'),
                'WRITE_MODE': p.get('write_mode', 'hset'),
                'BATCH_SIZE': str(p.get('pipeline_batch_size', 100)),
                'LWW_ENABLED': str(bool(p.get('lww_enabled', True))).lower(),
                'METRICS_PORT': '9090',
                # Inside the container the metrics server must accept the
                # published port; the publish below keeps it on host loopback.
                # Removed again for host networking, where it would be the host.
                'METRICS_BIND': '0.0.0.0',  # nosec B104 - container side of a 127.0.0.1 publish
                'ROW_FILTERS': p.get('transforms', '') or '',
                'CLOUDWATCH_ENABLED': 'true',
                'CLOUDWATCH_NAMESPACE': 'ECDL/Pipeline',
                'CLOUDWATCH_INTERVAL': '60'
            },
            'ports': ['127.0.0.1:9090:9090'],
            'networks': ['ecdl-network'],
            'restart': 'unless-stopped'
        }
        # Add replicas if configured
        sink_replicas = int(p.get('sink_replicas', 1) or 1)
        if sink_replicas > 1:
            sink_service['deploy'] = {'replicas': sink_replicas}
            # Remove container_name — can't use with replicas
            sink_service.pop('container_name', None)
            # Remove fixed port mapping — can't bind same port from multiple replicas
            sink_service.pop('ports', None)
        if is_msk_iam:
            broker_region = p.get('broker_region', 'us-east-1')
            sink_service['environment']['KAFKA_SECURITY_PROTOCOL'] = 'SASL_SSL'
            sink_service['environment']['KAFKA_SASL_MECHANISM'] = 'AWS_MSK_IAM'
            sink_service['environment']['AWS_DEFAULT_REGION'] = broker_region
            # Use host network so container can reach EC2 IMDS for instance profile credentials
            sink_service['network_mode'] = 'host'
            del sink_service['networks']
            del sink_service['ports']
            # Host networking: a wildcard bind would be every interface of the
            # host. Fall back to the template's loopback default.
            sink_service['environment'].pop('METRICS_BIND', None)
        if use_local_kafka:
            sink_service['depends_on'] = {'kafka': {'condition': 'service_healthy'}}
        if needs_host_gateway:
            sink_service['extra_hosts'] = ['host.docker.internal:host-gateway']
        compose['services']['valkey-sink'] = sink_service

        # Build Debezium connector JSON config
        debezium_connector = _debezium_connector(p, pipeline_name, topic_name,
                                                 db_engine, db_host)

        # MySQL-specific: add server.id and schema history
        if db_engine == 'mysql':
            debezium_connector['config']['database.server.id'] = str(abs(hash(pipeline_name)) % 100000 + 1)
            debezium_connector['config']['database.include.list'] = p.get('database_name', '')
            debezium_connector['config']['schema.history.internal.kafka.bootstrap.servers'] = kafka_bootstrap
            debezium_connector['config']['schema.history.internal.kafka.topic'] = f'ecdl.schema-history.{pipeline_name}'
            # Remove postgres-specific plugin.name for MySQL
            debezium_connector['config'].pop('plugin.name', None)

        sink_template_name = p.get('sink_template', 'valkey_default')
        sink_worker_code = _read_sink_template(sink_template_name)
        sink_dockerfile = SINK_DOCKERFILE
        sink_requirements = _sink_requirements(sink_template_name, is_msk_iam)

        # Generate startup script
        startup_script = f'''#!/bin/bash
set -e

echo "Starting ECDL Pipeline: {p['name']}"
echo "================================"

# Start the stack
docker-compose up -d

# Wait for Kafka Connect to be ready (up to 120s)
echo "Waiting for Debezium Connect to be ready..."
MAX_WAIT=120
WAITED=0
until curl -sf http://localhost:8083/ > /dev/null 2>&1; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "ERROR: Debezium Connect did not become ready within ${{MAX_WAIT}}s"
        echo "Check logs: docker-compose logs debezium-connect"
        exit 1
    fi
    echo "  Waiting... (${{WAITED}}s / ${{MAX_WAIT}}s)"
    sleep 5
    WAITED=$((WAITED + 5))
done
echo "Debezium Connect is ready!"

# Small extra delay for connector REST API to stabilize
sleep 5

# Register the connector
echo "Registering Debezium connector..."
curl -s -X POST -H "Content-Type: application/json" \\
    --data @connector-config.json \\
    http://localhost:8083/connectors

echo ""
echo "Pipeline deployed successfully!"
echo "  - Kafka: {kafka_bootstrap}"
echo "  - Debezium Connect: http://localhost:8083"
echo "  - Connector status: http://localhost:8083/connectors/ecdl-{pipeline_name}-connector/status"
'''

        # Generate stop script
        stop_script = f'''#!/bin/bash
echo "Stopping ECDL Pipeline: {p['name']}"

# Delete the connector first
curl -X DELETE http://localhost:8083/connectors/ecdl-{pipeline_name}-connector 2>/dev/null || true

# Stop containers
docker-compose down

echo "Pipeline stopped."
'''

        # Generate README
        readme = f'''# ECDL Pipeline: {p['name']}

This deployment package contains everything needed to run the CDC pipeline locally.

## Components

- **Zookeeper**: Kafka coordination
- **Kafka**: Message broker
- **Debezium Connect**: CDC connector for {db_engine}
- **Valkey Sink Worker**: Consumes from Kafka and writes to Valkey/ElastiCache

## Source Database
- Engine: {db_engine}
- Host: {p.get('host', 'N/A')}
- Database: {p.get('database_name', 'N/A')}
- Tables: {p.get('tables', 'N/A')}

## Target (Valkey/ElastiCache)
- Endpoint: {p.get('cluster_endpoint', 'N/A')}
- Port: {p.get('tgt_port', 6379)}
- Key Pattern: {p.get('key_pattern', '{{table}}:{{pk}}')}

## Quick Start

```bash
# Make scripts executable
chmod +x start.sh stop.sh

# Start the pipeline
./start.sh

# Check connector status
curl http://localhost:8083/connectors/ecdl-{pipeline_name}-connector/status

# View logs
docker-compose logs -f valkey-sink

# Stop the pipeline
./stop.sh
```

## Files

- `docker-compose.yml` - Container orchestration
- `connector-config.json` - Debezium connector configuration
- `sink-worker/` - Valkey sink worker source code
- `start.sh` - Startup script (registers connector)
- `stop.sh` - Shutdown script

## Monitoring

Check Debezium connector status:
```bash
curl http://localhost:8083/connectors/ecdl-{pipeline_name}-connector/status | jq
```

Sink worker metrics (JSON):
```bash
curl http://localhost:9090/metrics | jq
```

View Kafka topics:
```bash
docker exec ecdl-{pipeline_name}-kafka kafka-topics --list --bootstrap-server localhost:9092
```

View sink worker logs:
```bash
docker-compose logs -f valkey-sink
```
'''

        files = {
                'docker-compose.yml': yaml.dump(compose, default_flow_style=False, sort_keys=False),
                'connector-config.json': json.dumps(debezium_connector, indent=2),
                'sink-worker/sink_worker.py': sink_worker_code,
                'sink-worker/sink_common.py': _read_sink_common(),
                'sink-worker/Dockerfile': sink_dockerfile,
                'sink-worker/requirements.txt': sink_requirements,
                'start.sh': startup_script,
                'stop.sh': stop_script,
                'README.md': readme
        }

        # Add custom Debezium Dockerfile with MSK IAM auth JAR when needed
        if is_msk_iam:
            _add_msk_debezium_files(files, db_engine, kafka_bootstrap, pipeline_name)

        return jsonify({
            'success': True,
            'pipeline_name': p['name'],
            'files': files
        })
    except Exception as e:
        print(f"Error generating docker-compose: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>/download-deployment', methods=['GET'])
def download_deployment_package(id):
    """Download the deployment package as a ZIP file."""
    try:
        # Get the generated files
        with app.test_request_context():
            response = generate_docker_compose(id)
            if isinstance(response, tuple):
                return response
            data = response.get_json()

        if not data.get('success'):
            return jsonify({'error': data.get('error', 'Failed to generate')}), 500

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            pipeline_name = data['pipeline_name'].lower().replace(' ', '-')
            base_dir = f'ecdl-{pipeline_name}-deployment'

            for filename, content in data['files'].items():
                zf.writestr(f'{base_dir}/{filename}', content)

        zip_buffer.seek(0)
        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename=ecdl-{data["pipeline_name"]}-deployment.zip'
            }
        )
    except Exception as e:
        print(f"Error creating deployment package: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>/generate-ecs-fargate', methods=['GET'])
def generate_ecs_fargate(id):
    """Generate an ECS Fargate deployment package for a pipeline."""
    try:
        conn = get_db()
        pipeline = conn.execute('''
            SELECT p.*,
                   s.name as source_name, s.engine, s.host, s.port as src_port, s.database_name,
                   s.schema_name, s.username, s.password,
                   c.name as connector_name, c.connector_type, c.tables, c.snapshot_mode,
                   c.slot_name, c.publication_name,
                   b.name as broker_name, b.broker_type, b.bootstrap_servers, b.auth_type,
                   b.stream_name, b.region as broker_region, b.partitions,
                   t.name as target_name, t.cluster_endpoint, t.port as tgt_port, t.auth_token,
                   t.key_prefix, t.key_pattern, t.write_mode,
                   t.pipeline_batch_size, t.lww_enabled, t.ssl_enabled as tgt_ssl,
                   t.sink_template, t.dev_test
            FROM pipelines p
            LEFT JOIN sources s ON p.source_id = s.id
            LEFT JOIN connectors c ON p.connector_id = c.id
            LEFT JOIN brokers b ON p.broker_id = b.id
            LEFT JOIN targets t ON p.target_id = t.id
            WHERE p.id = ?
        ''', (id,)).fetchone()
        conn.close()

        if not pipeline:
            return jsonify({'error': 'Pipeline not found'}), 404

        p = dict(pipeline)
        pipeline_name = p['name'].lower().replace(' ', '-')
        topic_name = p['name'].replace(' ', '_')
        db_engine = p.get('engine', 'postgres')
        db_host = p.get('host', 'localhost')
        valkey_host = p.get('cluster_endpoint', 'localhost')
        bootstrap_servers = (p.get('bootstrap_servers') or '').strip()
        auth_type = (p.get('auth_type') or 'none').strip()
        is_msk_iam = auth_type == 'iam'
        broker_region = p.get('broker_region', 'us-east-1')
        kafka_bootstrap = bootstrap_servers if bootstrap_servers else 'localhost:9092'

        # Determine if we need to provision MSK or use existing
        use_existing_kafka = bootstrap_servers and bootstrap_servers not in ('localhost:9092', '127.0.0.1:9092')
        provision_msk = not use_existing_kafka

        sink_env = {
            'KAFKA_BOOTSTRAP_SERVERS': kafka_bootstrap,
            'KAFKA_CONSUMER_GROUP': f'ecdl-sink-{pipeline_name}-{int(time.time())}' if p.get('dev_test') else f'ecdl-sink-{pipeline_name}',
            'KAFKA_TOPIC_PATTERN': f'ecdl.{topic_name}.*',
            'VALKEY_HOST': valkey_host,
            'VALKEY_PORT': str(p.get('tgt_port', 6379)),
            'VALKEY_SSL': str(bool(p.get('tgt_ssl', True))).lower(),
            'VALKEY_AUTH_TOKEN': p.get('auth_token', ''),
            'KEY_PREFIX': p.get('key_prefix', ''),
            'KEY_PATTERN': p.get('key_pattern', '{table}:{pk}'),
            'WRITE_MODE': p.get('write_mode', 'hset'),
            'BATCH_SIZE': str(p.get('pipeline_batch_size', 100)),
            'LWW_ENABLED': str(bool(p.get('lww_enabled', True))).lower(),
            'METRICS_PORT': '9090',
        }
        if is_msk_iam:
            sink_env['KAFKA_SECURITY_PROTOCOL'] = 'SASL_SSL'
            sink_env['KAFKA_SASL_MECHANISM'] = 'AWS_MSK_IAM'
            sink_env['AWS_DEFAULT_REGION'] = broker_region

        debezium_env = _connect_worker_env(kafka_bootstrap, pipeline_name)
        if is_msk_iam:
            debezium_env['AWS_REGION'] = broker_region
            debezium_env['AWS_DEFAULT_REGION'] = broker_region
            debezium_env.update(_connect_worker_iam_env())

        debezium_connector = _debezium_connector(p, pipeline_name, topic_name,
                                                 db_engine, db_host)
        if is_msk_iam:
            history = {"database.history.kafka.bootstrap.servers": kafka_bootstrap}
            for client in ('consumer', 'producer'):
                history.update(_msk_iam_client_settings(
                    'database.history.{}.'.format(client)))
            debezium_connector['config'].update(history)

        sink_template_name = p.get('sink_template', 'valkey_default')
        sink_worker_code = _read_sink_template(sink_template_name)
        sink_dockerfile = SINK_DOCKERFILE
        sink_requirements = _sink_requirements(sink_template_name, is_msk_iam)

        def _cfn_env(env_dict):
            return [{"Name": k, "Value": str(v) if v is not None else ''} for k, v in env_dict.items()]

        debezium_image = _debezium_image(db_engine)

        cfn = {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": f"ECDL ECS Fargate deployment for pipeline: {p['name']}",
            "Parameters": {
                "VpcId": {"Type": "AWS::EC2::VPC::Id", "Description": "VPC to deploy ECS tasks into"},
                "SubnetIds": {"Type": "List<AWS::EC2::Subnet::Id>", "Description": "Private subnets for ECS tasks"},
                "SinkWorkerImage": {"Type": "String", "Description": "ECR image URI for the sink worker container"},
                "DebeziumImage": {"Type": "String", "Default": debezium_image, "Description": "Debezium Connect container image"},
            },
            "Resources": {
                "ECSCluster": {
                    "Type": "AWS::ECS::Cluster",
                    "Properties": {
                        "ClusterName": f"ecdl-{pipeline_name}",
                        "ClusterSettings": [{"Name": "containerInsights", "Value": "enabled"}]
                    }
                },
                "SinkLogGroup": {
                    "Type": "AWS::Logs::LogGroup",
                    "Properties": {"LogGroupName": f"/ecs/ecdl-{pipeline_name}-sink", "RetentionInDays": 14}
                },
                "DebeziumLogGroup": {
                    "Type": "AWS::Logs::LogGroup",
                    "Properties": {"LogGroupName": f"/ecs/ecdl-{pipeline_name}-debezium", "RetentionInDays": 14}
                },
                "TaskExecutionRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "RoleName": f"ecdl-{pipeline_name}-exec-role",
                        "AssumeRolePolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]
                        },
                        "ManagedPolicyArns": ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"]
                    }
                },
                "TaskRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "RoleName": f"ecdl-{pipeline_name}-task-role",
                        "AssumeRolePolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]
                        },
                        "Policies": [{
                            "PolicyName": "MSKAccess",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [{"Effect": "Allow", "Action": [
                                    "kafka-cluster:Connect", "kafka-cluster:DescribeCluster",
                                    "kafka-cluster:DescribeTopic", "kafka-cluster:ReadData",
                                    "kafka-cluster:WriteData", "kafka-cluster:CreateTopic",
                                    "kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"
                                ], "Resource": "*"}]
                            }
                        }] if is_msk_iam else [{
                            "PolicyName": "Logs",
                            "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "logs:CreateLogStream", "Resource": "*"}]}
                        }]
                    }
                },
                "ECSSecurityGroup": {
                    "Type": "AWS::EC2::SecurityGroup",
                    "Properties": {
                        "GroupDescription": f"ECDL {p['name']} ECS tasks",
                        "VpcId": {"Ref": "VpcId"},
                        "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0", "Description": "Allow all outbound"}]
                    }
                },
                "SinkTaskDefinition": {
                    "Type": "AWS::ECS::TaskDefinition",
                    "Properties": {
                        "Family": f"ecdl-{pipeline_name}-sink",
                        "Cpu": "512", "Memory": "1024",
                        "NetworkMode": "awsvpc",
                        "RequiresCompatibilities": ["FARGATE"],
                        "ExecutionRoleArn": {"Fn::GetAtt": ["TaskExecutionRole", "Arn"]},
                        "TaskRoleArn": {"Fn::GetAtt": ["TaskRole", "Arn"]},
                        "ContainerDefinitions": [{
                            "Name": "sink-worker",
                            "Image": {"Ref": "SinkWorkerImage"},
                            "Essential": True,
                            "Environment": _cfn_env(sink_env),
                            "PortMappings": [{"ContainerPort": 9090, "Protocol": "tcp"}],
                            "LogConfiguration": {
                                "LogDriver": "awslogs",
                                "Options": {
                                    "awslogs-group": {"Ref": "SinkLogGroup"},
                                    "awslogs-region": {"Ref": "AWS::Region"},
                                    "awslogs-stream-prefix": "sink"
                                }
                            }
                        }]
                    }
                },
                "DebeziumTaskDefinition": {
                    "Type": "AWS::ECS::TaskDefinition",
                    "Properties": {
                        "Family": f"ecdl-{pipeline_name}-debezium",
                        "Cpu": "1024", "Memory": "2048",
                        "NetworkMode": "awsvpc",
                        "RequiresCompatibilities": ["FARGATE"],
                        "ExecutionRoleArn": {"Fn::GetAtt": ["TaskExecutionRole", "Arn"]},
                        "TaskRoleArn": {"Fn::GetAtt": ["TaskRole", "Arn"]},
                        "ContainerDefinitions": [{
                            "Name": "debezium-connect",
                            "Image": {"Ref": "DebeziumImage"},
                            "Essential": True,
                            "Environment": _cfn_env(debezium_env),
                            "PortMappings": [{"ContainerPort": 8083, "Protocol": "tcp"}],
                            "LogConfiguration": {
                                "LogDriver": "awslogs",
                                "Options": {
                                    "awslogs-group": {"Ref": "DebeziumLogGroup"},
                                    "awslogs-region": {"Ref": "AWS::Region"},
                                    "awslogs-stream-prefix": "debezium"
                                }
                            }
                        }]
                    }
                },
                "DebeziumService": {
                    "Type": "AWS::ECS::Service",
                    "DependsOn": ["DebeziumLogGroup"],
                    "Properties": {
                        "ServiceName": f"ecdl-{pipeline_name}-debezium",
                        "Cluster": {"Ref": "ECSCluster"},
                        "TaskDefinition": {"Ref": "DebeziumTaskDefinition"},
                        "DesiredCount": 1, "LaunchType": "FARGATE",
                        "NetworkConfiguration": {"AwsvpcConfiguration": {
                            "Subnets": {"Ref": "SubnetIds"},
                            "SecurityGroups": [{"Ref": "ECSSecurityGroup"}],
                            "AssignPublicIp": "ENABLED"
                        }}
                    }
                },
                "SinkService": {
                    "Type": "AWS::ECS::Service",
                    "DependsOn": ["DebeziumService", "SinkLogGroup"],
                    "Properties": {
                        "ServiceName": f"ecdl-{pipeline_name}-sink",
                        "Cluster": {"Ref": "ECSCluster"},
                        "TaskDefinition": {"Ref": "SinkTaskDefinition"},
                        "DesiredCount": 1, "LaunchType": "FARGATE",
                        "NetworkConfiguration": {"AwsvpcConfiguration": {
                            "Subnets": {"Ref": "SubnetIds"},
                            "SecurityGroups": [{"Ref": "ECSSecurityGroup"}],
                            "AssignPublicIp": "ENABLED"
                        }}
                    }
                },
            },
            "Outputs": {
                "ClusterName": {"Value": {"Ref": "ECSCluster"}, "Description": "ECS Cluster name"},
                "SinkServiceName": {"Value": {"Fn::GetAtt": ["SinkService", "Name"]}, "Description": "Sink worker ECS service"},
                "DebeziumServiceName": {"Value": {"Fn::GetAtt": ["DebeziumService", "Name"]}, "Description": "Debezium Connect ECS service"},
            }
        }

        # Conditionally add MSK cluster if no external Kafka provided
        if provision_msk:
            cfn["Resources"]["MSKCluster"] = {
                "Type": "AWS::MSK::Cluster",
                "Properties": {
                    "ClusterName": f"ecdl-{pipeline_name}-msk",
                    "KafkaVersion": "3.6.0",
                    "NumberOfBrokerNodes": 2,
                    "BrokerNodeGroupInfo": {
                        "InstanceType": "kafka.t3.small",
                        "ClientSubnets": {"Ref": "SubnetIds"},
                        "SecurityGroups": [{"Ref": "ECSSecurityGroup"}],
                        "StorageInfo": {"EBSStorageInfo": {"VolumeSize": 50}}
                    },
                    "ConfigurationInfo": {
                        "Arn": {"Ref": "MSKConfiguration"},
                        "Revision": 1
                    }
                }
            }
            cfn["Resources"]["MSKConfiguration"] = {
                "Type": "AWS::MSK::Configuration",
                "Properties": {
                    "Name": f"ecdl-{pipeline_name}-config",
                    "ServerProperties": "auto.create.topics.enable=true\ndefault.replication.factor=2\nnum.partitions=" + str(p.get('partitions', 5))
                }
            }
            # Add MSK bootstrap servers to Outputs
            cfn["Outputs"]["MSKBootstrapServers"] = {
                "Value": {"Fn::GetAtt": ["MSKCluster", "BootstrapBrokerString"]},
                "Description": "MSK bootstrap servers (update task definitions with this after stack creation)"
            }
            # Add note to deploy script
            cfn["Metadata"] = {
                "ECDL": {
                    "MSKProvisioned": True,
                    "Note": "MSK cluster included. After stack creation, update KAFKA_BOOTSTRAP_SERVERS in task definitions with the MSK output value."
                }
            }
            # Make DebeziumService depend on MSK
            cfn["Resources"]["DebeziumService"]["DependsOn"] = ["DebeziumLogGroup", "MSKCluster"]
        else:
            cfn["Metadata"] = {
                "ECDL": {
                    "MSKProvisioned": False,
                    "ExistingKafka": kafka_bootstrap,
                    "Note": "Using existing Kafka/MSK. No MSK cluster created."
                }
            }

        # Read optional vpc_id / subnet_ids from query params
        qs_vpc_id = request.args.get('vpc_id', '').strip()
        qs_subnet_ids = request.args.get('subnet_ids', '').strip()

        deploy_script = '#!/bin/bash\nset -e\n\n'
        deploy_script += f'STACK_NAME="ecdl-{pipeline_name}"\n'
        deploy_script += f'REGION="${{AWS_DEFAULT_REGION:-{broker_region}}}"\n\n'
        if qs_vpc_id and qs_subnet_ids:
            deploy_script += f'# VPC / Subnet values from ECDL deployment config\n'
            deploy_script += f'VPC_ID="${{VPC_ID:-{qs_vpc_id}}}"\n'
            deploy_script += f'SUBNET_IDS="${{SUBNET_IDS:-{qs_subnet_ids}}}"\n\n'
        else:
            deploy_script += '# ---- Required: set these to your VPC / subnet values ----\n'
            deploy_script += 'VPC_ID="${VPC_ID:-}"\n'
            deploy_script += 'SUBNET_IDS="${SUBNET_IDS:-}"  # comma-separated, e.g. subnet-aaa,subnet-bbb\n\n'
        deploy_script += 'if [ -z "$VPC_ID" ] || [ -z "$SUBNET_IDS" ]; then\n'
        deploy_script += '  echo "ERROR: VPC_ID and SUBNET_IDS environment variables are required."\n'
        deploy_script += '  echo "Usage:"\n'
        deploy_script += '  echo "  export VPC_ID=vpc-xxxxxxxxx"\n'
        deploy_script += '  echo "  export SUBNET_IDS=subnet-aaa,subnet-bbb"\n'
        deploy_script += '  echo "  ./deploy.sh"\n'
        deploy_script += '  exit 1\n'
        deploy_script += 'fi\n\n'
        deploy_script += f'echo "=== ECDL ECS Fargate Deployment: {p["name"]} ==="\n'
        deploy_script += 'echo "  VPC:     $VPC_ID"\n'
        deploy_script += 'echo "  Subnets: $SUBNET_IDS"\necho ""\n\n'
        deploy_script += 'ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)\n'
        deploy_script += f'ECR_REPO_SINK="${{ACCOUNT_ID}}.dkr.ecr.${{REGION}}.amazonaws.com/ecdl-{pipeline_name}-sink"\n'
        if is_msk_iam:
            deploy_script += f'ECR_REPO_DEBEZIUM="${{ACCOUNT_ID}}.dkr.ecr.${{REGION}}.amazonaws.com/ecdl-{pipeline_name}-debezium"\n'
        deploy_script += '\n'
        deploy_script += 'echo "Step 1: Creating ECR repositories (if needed)..."\n'
        deploy_script += f'aws ecr create-repository --repository-name ecdl-{pipeline_name}-sink --region $REGION 2>/dev/null || true\n'
        if is_msk_iam:
            deploy_script += f'aws ecr create-repository --repository-name ecdl-{pipeline_name}-debezium --region $REGION 2>/dev/null || true\n'
        deploy_script += '\n'
        deploy_script += 'echo "Step 2: Logging into ECR..."\n'
        deploy_script += 'aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com\n\n'
        deploy_script += f'echo "Step 3: Building and pushing sink worker image..."\n'
        deploy_script += f'docker build --platform linux/amd64 -t ecdl-{pipeline_name}-sink ./sink-worker\n'
        deploy_script += f'docker tag ecdl-{pipeline_name}-sink:latest $ECR_REPO_SINK:latest\n'
        deploy_script += 'docker push $ECR_REPO_SINK:latest\n\n'
        if is_msk_iam:
            deploy_script += f'echo "Step 4: Building and pushing custom Debezium image (with MSK IAM auth)..."\n'
            deploy_script += f'docker build --platform linux/amd64 -t ecdl-{pipeline_name}-debezium ./debezium-connect\n'
            deploy_script += f'docker tag ecdl-{pipeline_name}-debezium:latest $ECR_REPO_DEBEZIUM:latest\n'
            deploy_script += 'docker push $ECR_REPO_DEBEZIUM:latest\n\n'
            deploy_script += 'echo "Step 5: Deploying CloudFormation stack..."\n'
        else:
            deploy_script += 'echo "Step 4: Deploying CloudFormation stack..."\n'
        deploy_script += 'aws cloudformation deploy \\\n'
        deploy_script += '    --template-file cloudformation.json \\\n'
        deploy_script += '    --stack-name $STACK_NAME \\\n'
        deploy_script += '    --region $REGION \\\n'
        deploy_script += '    --capabilities CAPABILITY_NAMED_IAM \\\n'
        deploy_script += '    --parameter-overrides \\\n'
        deploy_script += '        VpcId=$VPC_ID \\\n'
        deploy_script += '        SubnetIds=$SUBNET_IDS \\\n'
        deploy_script += '        SinkWorkerImage=$ECR_REPO_SINK:latest'
        if is_msk_iam:
            deploy_script += ' \\\n        DebeziumImage=$ECR_REPO_DEBEZIUM:latest'
        deploy_script += '\n\n'
        deploy_script += 'echo ""\necho "Step 5: Register Debezium connector"\n'
        deploy_script += 'echo "  Find the Debezium task private IP in the ECS console, then run:"\n'
        deploy_script += 'echo "  curl -X POST -H Content-Type:application/json --data @connector-config.json http://<DEBEZIUM_IP>:8083/connectors"\n'
        deploy_script += 'echo ""\necho "Deployment complete!"\n'
        deploy_script += f'echo "  Console: https://${{REGION}}.console.aws.amazon.com/ecs/v2/clusters/ecdl-{pipeline_name}/services"\n'

        teardown_script = '#!/bin/bash\nset -e\n\n'
        teardown_script += f'STACK_NAME="ecdl-{pipeline_name}"\n'
        teardown_script += f'REGION="${{AWS_DEFAULT_REGION:-{broker_region}}}"\n\n'
        teardown_script += 'echo "Tearing down ECDL ECS stack: $STACK_NAME"\n'
        teardown_script += 'aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION\n'
        teardown_script += 'echo "Stack deletion initiated. Monitor in CloudFormation console."\n'

        readme = f'# ECDL ECS Fargate Deployment: {p["name"]}\n\n'
        readme += '## Prerequisites\n\n'
        readme += '- AWS CLI configured with appropriate permissions\n'
        readme += '- Docker installed (for building the sink worker image)\n'
        readme += '- A VPC with private subnets that can reach your MSK cluster and ElastiCache\n\n'
        readme += '## Components\n\n'
        readme += '| Service | CPU | Memory | Description |\n'
        readme += '|---------|-----|--------|-------------|\n'
        readme += f'| Debezium Connect | 1 vCPU | 2 GB | CDC connector for {db_engine} |\n'
        readme += '| Valkey Sink Worker | 0.5 vCPU | 1 GB | Consumes from Kafka, writes to Valkey |\n\n'
        readme += f'## Source: {db_engine} @ {db_host}\n'
        readme += f'## Target: {valkey_host}:{p.get("tgt_port", 6379)} ({p.get("write_mode", "hset")})\n\n'
        readme += '## Quick Start\n\n```bash\nchmod +x deploy.sh teardown.sh\nexport VPC_ID=vpc-xxxxxxxxx\nexport SUBNET_IDS=subnet-aaa,subnet-bbb\n./deploy.sh\n```\n\n'
        readme += '## Files\n\n'
        readme += '- `cloudformation.json` - CloudFormation template\n'
        readme += '- `connector-config.json` - Debezium connector config\n'
        readme += '- `sink-worker/` - Sink worker source + Dockerfile\n'
        readme += '- `deploy.sh` - Build, push to ECR, deploy CFN\n'
        readme += '- `teardown.sh` - Delete the CFN stack\n\n'
        readme += '## Register Debezium Connector\n\n'
        readme += 'After deploy, find the Debezium task private IP in ECS console:\n\n'
        readme += '```bash\ncurl -X POST -H "Content-Type: application/json" \\\n'
        readme += '    --data @connector-config.json \\\n'
        readme += '    http://<DEBEZIUM_PRIVATE_IP>:8083/connectors\n```\n\n'
        readme += '## Monitoring\n\n```bash\n'
        readme += f'aws logs tail /ecs/ecdl-{pipeline_name}-sink --follow --region {broker_region}\n'
        readme += f'aws logs tail /ecs/ecdl-{pipeline_name}-debezium --follow --region {broker_region}\n```\n'

        files = {
            'cloudformation.json': json.dumps(cfn, indent=2),
            'connector-config.json': json.dumps(debezium_connector, indent=2),
            'sink-worker/sink_worker.py': sink_worker_code,
            'sink-worker/sink_common.py': _read_sink_common(),
            'sink-worker/Dockerfile': sink_dockerfile,
            'sink-worker/requirements.txt': sink_requirements,
            'deploy.sh': deploy_script,
            'teardown.sh': teardown_script,
            'README.md': readme,
        }

        # Add custom Debezium Dockerfile with MSK IAM auth JAR when needed
        if is_msk_iam:
            _add_msk_debezium_files(files, db_engine, kafka_bootstrap, pipeline_name)

        return jsonify({
            'success': True,
            'pipeline_name': p['name'],
            'files': files
        })
    except Exception as e:
        print(f"Error generating ECS Fargate package: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>/download-ecs-deployment', methods=['GET'])
def download_ecs_deployment(id):
    """Download the ECS Fargate deployment package as a ZIP file."""
    try:
        # Forward query params (vpc_id, subnet_ids) to the generator
        qs = request.query_string.decode()
        ctx_path = f'/api/pipelines/{id}/generate-ecs-fargate'
        if qs:
            ctx_path += f'?{qs}'
        with app.test_request_context(ctx_path):
            response = generate_ecs_fargate(id)
            if isinstance(response, tuple):
                return response
            data = response.get_json()

        if not data.get('success'):
            return jsonify({'error': data.get('error', 'Failed to generate')}), 500

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            pipeline_name = data['pipeline_name'].lower().replace(' ', '-')
            base_dir = f'ecdl-{pipeline_name}-ecs-deployment'
            for filename, content in data['files'].items():
                zf.writestr(f'{base_dir}/{filename}', content)

        zip_buffer.seek(0)
        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename=ecdl-{data["pipeline_name"]}-ecs-deployment.zip'
            }
        )
    except Exception as e:
        print(f"Error creating ECS deployment package: {e}")
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


@app.route('/api/maintenance/restore', methods=['POST'])
def restore_database():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        temp_path = DATABASE_PATH + '.restore_tmp'
        file.save(temp_path)

        try:
            test_conn = sqlite3.connect(temp_path)
            test_conn.execute('SELECT count(*) FROM sqlite_master')
            test_conn.close()
        except Exception as e:
            os.remove(temp_path)
            return jsonify({'error': f'Invalid SQLite database: {str(e)}'}), 400

        shutil.move(temp_path, DATABASE_PATH)
        return jsonify({'success': True})
    except Exception as e:
        temp_path = DATABASE_PATH + '.restore_tmp'
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({'error': str(e)}), 500


# ============================================================
# RANDOMIZER – data generator for testing
# ============================================================

import threading
import random
import string
import time as _time

import repo_path  # noqa: E402,F401  (puts ecdl_link on sys.path)
from ecdl_link import sqlident  # noqa: E402

_randomizer_threads = {}   # keyed by source_id
_randomizer_stop = {}      # Event flags


def _connect_source_db(src):
    """Return a DB-API connection for the given source row dict."""
    engine = src['engine']
    if engine == 'postgres':
        import psycopg2
        return psycopg2.connect(
            host=src['host'], port=src['port'],
            dbname=src['database_name'], user=src['username'],
            password=src['password'],
            sslmode=src.get('ssl_mode', 'require')
        )
    elif engine == 'mysql':
        import pymysql
        return pymysql.connect(
            host=src['host'], port=src['port'],
            database=src['database_name'], user=src['username'],
            password=src['password']
        )
    else:
        raise ValueError(f"Unsupported engine: {engine}")


@app.route('/api/sources/<int:id>/tables', methods=['GET'])
def list_source_tables(id):
    """Connect to the source DB and return its table list."""
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
    except Exception as e:
        print(f"Error listing tables: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sources/<int:id>/columns', methods=['GET'])
def list_source_columns(id):
    """Return column names + types for a given table."""
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


def _randomizer_worker(source_id, table, columns, batch_size, interval, schema):
    """Background thread that inserts random rows."""
    stop_evt = _randomizer_stop.get(source_id)
    status = _randomizer_threads[source_id]
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

        while not stop_evt.is_set():
            for _ in range(batch_size):
                if stop_evt.is_set():
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
            stop_evt.wait(interval)

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
    return {'source_id': data.get('source_id'), 'table': table, 'schema': schema,
            'columns': columns, 'batch_size': batch_size, 'interval': interval}


@app.route('/api/randomizer/start', methods=['POST'])
def randomizer_start():
    """Start the background data generator for a source."""
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
        }
        _randomizer_threads[source_id] = status

        t = threading.Thread(
            target=_randomizer_worker,
            args=(source_id, table, columns, batch_size, interval, schema),
            daemon=True
        )
        t.start()
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/randomizer/stop', methods=['POST'])
def randomizer_stop():
    """Stop the background data generator."""
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


@app.route('/api/pipelines/<int:id>/deploy-local', methods=['POST'])
def deploy_local(id):
    """Deploy pipeline locally on the EC2 — writes files to disk and runs start.sh."""
    try:
        with app.test_request_context():
            response = generate_docker_compose(id)
            if isinstance(response, tuple):
                return response
            data = response.get_json()

        if not data.get('success'):
            return jsonify({'error': data.get('error', 'Failed to generate')}), 500

        pipeline_name = data['pipeline_name'].lower().replace(' ', '-')
        deploy_dir = os.path.join('/app/data', 'deployments', f'ecdl-{pipeline_name}')
        os.makedirs(deploy_dir, exist_ok=True)

        for filename, content in data['files'].items():
            filepath = os.path.join(deploy_dir, filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            # Patch docker-compose.yml to use existing app network instead of isolated network
            if filename == 'docker-compose.yml':
                content = content.replace('BOOTSTRAP_SERVERS: localhost:9092', 'BOOTSTRAP_SERVERS: kafka:29092')
                content = content.replace('KAFKA_BOOTSTRAP_SERVERS: localhost:9092', 'KAFKA_BOOTSTRAP_SERVERS: kafka:29092')
                # Replace the generated network with the existing app network
                import re
                content = re.sub(
                    r'networks:\n\s+ecdl-network:\n\s+driver: bridge',
                    'networks:\n  ecdl-network:\n    external: true\n    name: elasticache-data-link_default',
                    content
                )
            if filename == 'connector-config.json':
                content = content.replace('"localhost:9092"', '"kafka:29092"')
                # Fix table.include.list for MySQL (replace public.X with Database.X)
                content = content.replace('"public.', '"Inventory.')
                # Fix replication factor for single-broker Kafka
                content = content.replace('"topic.creation.default.replication.factor": "3"', '"topic.creation.default.replication.factor": "1"')
                content = content.replace('"topic.creation.default.partitions": "3"', '"topic.creation.default.partitions": "1"')
            with open(filepath, 'w') as f:
                f.write(content)

        # Make scripts executable
        for script in ['start.sh', 'stop.sh']:
            script_path = os.path.join(deploy_dir, script)
            if os.path.exists(script_path):
                # Owner only: the generated scripts can carry the source
                # database password in the connector config they POST.
                os.chmod(script_path, 0o700)

        # Files are written to /app/data/deployments/ which is mounted from the host
        # To run: ssh into EC2 and execute start.sh from the deployment directory
        host_deploy_dir = deploy_dir.replace('/app/data', '/home/ec2-user/elasticache-data-link/data')

        # Update pipeline status
        conn = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("UPDATE pipelines SET status='deploying', last_deployed=? WHERE id=?", (now, id))
        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'deploy_dir': host_deploy_dir,
            'message': f'Deployment files written. Run start.sh from: {host_deploy_dir}'
        })
    except Exception as e:
        print(f"Error deploying locally: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>/deploy-local/status', methods=['GET'])
def deploy_local_status(id):
    """Check local deployment status by reading the deploy log."""
    try:
        conn = get_db()
        pipeline = conn.execute('SELECT name FROM pipelines WHERE id=?', (id,)).fetchone()
        conn.close()
        if not pipeline:
            return jsonify({'error': 'Pipeline not found'}), 404

        pipeline_name = pipeline['name'].lower().replace(' ', '-')
        deploy_dir = os.path.join('/app/data', 'deployments', f'ecdl-{pipeline_name}')
        log_path = os.path.join(deploy_dir, 'deploy.log')
        host_deploy_dir = deploy_dir.replace('/app/data', '/home/ec2-user/elasticache-data-link/data')

        log_content = ''
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                log_content = f.read()

        return jsonify({'success': True, 'log': log_content, 'deploy_dir': host_deploy_dir})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/connectors/<int:id>/apply-changes', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=60)
def apply_connector_changes(id):
    """Apply connector config changes: updates Debezium table list AND restarts sink worker with new transforms."""
    try:
        data = request.json or {}
        instance_id = data.get('instance_id', '')

        if not instance_id:
            return jsonify({'error': 'instance_id is required. Provide the EC2 instance ID where the pipeline is running.'}), 400

        # Get full connector + source + pipeline info
        conn = get_db()
        connector = conn.execute('''
            SELECT c.*, s.host, s.port, s.database_name, s.username, s.password, s.engine
            FROM connectors c
            LEFT JOIN sources s ON c.source_id = s.id
            WHERE c.id = ?
        ''', (id,)).fetchone()
        conn.close()
        if not connector:
            return jsonify({'error': 'Connector not found'}), 404

        c = dict(connector)
        transforms = c.get('transforms') or '{}'
        tables = c.get('tables', '')
        db_engine = c.get('engine', 'mysql')
        database_name = c.get('database_name', '')

        # Qualify table names for MySQL
        if db_engine == 'mysql' and tables:
            qualified = []
            for t in tables.split(','):
                t = t.strip()
                if t and '.' not in t:
                    qualified.append(f"{database_name}.{t}")
                else:
                    qualified.append(t)
            tables = ','.join(qualified)

        # Send command to EC2 to:
        # 1. Update Debezium connector table list
        # 2. Restart sink worker with new transforms
        import boto3
        ssm = boto3.client('ssm', region_name=os.getenv('AWS_DEFAULT_REGION', 'us-east-2'))

        # Escape transforms for shell
        transforms_escaped = transforms.replace('"', '\\"')

        commands = [
            'echo "=== Applying Changes ==="',
            'echo "Step 1: Updating Debezium connector table list..."',
            # Find the running Debezium connector name
            'DEBEZIUM_CONNECTOR=$(curl -s localhost:8083/connectors/ | python3 -c "import json,sys; connectors=json.load(sys.stdin); print(connectors[0] if connectors else \'\')" 2>/dev/null)',
            'if [ -z "$DEBEZIUM_CONNECTOR" ]; then echo "No Debezium connector found"; else',
            # Get current config and update table list
            f'  curl -s localhost:8083/connectors/$DEBEZIUM_CONNECTOR/config > /tmp/connector_config.json',
            f'  python3 -c "import json; c=json.load(open(\'/tmp/connector_config.json\')); c[\'table.include.list\']=\'{tables}\'; json.dump(c, open(\'/tmp/connector_config_new.json\',\'w\'))"',
            '  curl -s -X PUT http://localhost:8083/connectors/$DEBEZIUM_CONNECTOR/config -H "Content-Type: application/json" -d @/tmp/connector_config_new.json > /dev/null',
            '  echo "  Updated table list to: ' + tables[:80] + '"',
            'fi',
            '',
            'echo "Step 2: Restarting sink worker with new transforms..."',
            'SINK_CONTAINER=$(docker ps --format "{{.Names}}" | grep sink | head -1)',
            'if [ -z "$SINK_CONTAINER" ]; then echo "No sink worker found"; exit 1; fi',
            # Get current container env and image
            'SINK_IMAGE=$(docker inspect --format "{{.Config.Image}}" $SINK_CONTAINER)',
            'docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $SINK_CONTAINER > /tmp/sink_env.txt',
            # Update ROW_FILTERS
            'grep -v "^ROW_FILTERS=" /tmp/sink_env.txt > /tmp/sink_env_new.txt',
            f'echo \'ROW_FILTERS={transforms}\' >> /tmp/sink_env_new.txt',
            # Restart sink worker
            'docker update --restart no $SINK_CONTAINER 2>/dev/null',
            'docker stop $SINK_CONTAINER && docker rm $SINK_CONTAINER',
            'docker run -d --name $SINK_CONTAINER --network host --restart unless-stopped --env-file /tmp/sink_env_new.txt '
            '-v /home/ec2-user/ecdl-pipeline/sink_worker.py:/app/sink_worker.py '
            '$SINK_IMAGE',
            '',
            'echo "=== Done. Both Debezium and sink worker updated. ==="',
        ]

        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': commands},
            TimeoutSeconds=90
        )
        command_id = response['Command']['CommandId']

        audit_log('apply_changes', 'connector', id, None, f'Applied to {instance_id}: tables={tables[:50]}, transforms updated')

        return jsonify({
            'success': True,
            'message': f'Applying changes to {instance_id}: Debezium table list updated + sink worker restarting with new transforms.',
            'command_id': command_id,
            'instance_id': instance_id
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipelines/<int:id>/deploy-cloud', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=60)
def deploy_cloud(id):
    """Deploy pipeline to a new EC2 instance or ECS Fargate."""
    try:
        data = request.json or {}
        deploy_target = data.get('deploy_target', 'ec2')
        region = data.get('region', 'us-east-2')

        # Get pipeline details
        conn = get_db()
        pipeline = conn.execute('''
            SELECT p.*, s.host as source_host, s.port as source_port, s.database_name,
                   s.username as db_user, s.password as db_pass, s.engine as source_engine,
                   c.tables as connector_tables, c.snapshot_mode, c.transforms,
                   b.bootstrap_servers, b.partitions,
                   t.cluster_endpoint as valkey_host, t.port as valkey_port,
                   t.ssl_enabled, t.key_pattern, t.write_mode, t.pipeline_batch_size,
                   t.lww_enabled, t.sink_replicas
            FROM pipelines p
            JOIN sources s ON p.source_id = s.id
            JOIN connectors c ON p.connector_id = c.id
            JOIN brokers b ON p.broker_id = b.id
            JOIN targets t ON p.target_id = t.id
            WHERE p.id = ?
        ''', (id,)).fetchone()
        conn.close()

        if not pipeline:
            return jsonify({'error': 'Pipeline not found'}), 404

        p = dict(pipeline)
        pipeline_name = p['name'].lower().replace(' ', '-')

        if deploy_target == 'ec2':
            # Check if deploying to existing EC2 or new one
            existing_instance_id = data.get('existing_instance_id', '').strip()
            if existing_instance_id:
                return _deploy_to_existing_ec2(p, pipeline_name, data, region, id, existing_instance_id)
            return _deploy_ec2(p, pipeline_name, data, region, id)
        elif deploy_target == 'ecs':
            return _deploy_ecs(p, pipeline_name, data, region, id)
        else:
            return jsonify({'error': f'Unknown deploy target: {deploy_target}'}), 400

    except Exception as e:
        print(f"Deploy cloud error: {e}")
        return jsonify({'error': str(e)}), 500


def _deploy_ec2(p, pipeline_name, config, region, pipeline_id):
    """Launch a new EC2 instance with the ECDL pipeline via user-data."""
    import boto3

    instance_type = config.get('instance_type', 'c5.xlarge')
    vpc_id = config.get('vpc_id', '')
    subnet_id = config.get('subnet_id', '')
    security_group_id = config.get('security_group_id', '')

    client = boto3.client('ec2', region_name=region)

    # Build user-data script that installs Docker and starts the pipeline
    user_data = f'''#!/bin/bash
set -e
exec > /var/log/ecdl-deploy.log 2>&1

echo "=== ECDL Pipeline Deployment ==="
echo "Pipeline: {pipeline_name}"
echo "Started: $(date)"

# Install Docker
yum update -y
yum install -y docker python3-pip
systemctl start docker
systemctl enable docker
usermod -aG docker ec2-user

# Install Docker Compose
curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Install Python dependencies
pip3 install pymysql valkey kafka-python boto3

# Create deployment directory
mkdir -p /home/ec2-user/ecdl-pipeline
cd /home/ec2-user/ecdl-pipeline

# Write docker-compose.yml
cat > docker-compose.yml << 'COMPOSE'
version: "3.8"
services:
  zookeeper:
    image: confluentinc/cp-zookeeper:7.6.0
    container_name: ecdl-zookeeper
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
    restart: unless-stopped

  kafka:
    image: confluentinc/cp-kafka:7.6.0
    container_name: ecdl-kafka
    depends_on:
      - zookeeper
    ports:
      - "127.0.0.1:9092:9092"
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT
      KAFKA_INTER_BROKER_LISTENER_ID: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      KAFKA_NUM_PARTITIONS: {p.get('partitions', 5)}
    restart: unless-stopped

  debezium:
    image: debezium/connect:2.5
    container_name: ecdl-debezium
    depends_on:
      - kafka
    ports:
      - "127.0.0.1:8083:8083"
    environment:
      BOOTSTRAP_SERVERS: kafka:29092
      GROUP_ID: ecdl-{pipeline_name}
      CONFIG_STORAGE_TOPIC: ecdl_{pipeline_name}_configs
      OFFSET_STORAGE_TOPIC: ecdl_{pipeline_name}_offsets
      STATUS_STORAGE_TOPIC: ecdl_{pipeline_name}_status
      CONFIG_STORAGE_REPLICATION_FACTOR: 1
      OFFSET_STORAGE_REPLICATION_FACTOR: 1
      STATUS_STORAGE_REPLICATION_FACTOR: 1
    restart: unless-stopped

  sink-worker:
    image: public.ecr.aws/docker/library/python:3.11-slim
    container_name: ecdl-sink
    depends_on:
      - kafka
    environment:
      KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      KAFKA_CONSUMER_GROUP: ecdl-sink-{pipeline_name}
      KAFKA_TOPIC_PATTERN: "ecdl.{pipeline_name}.*"
      VALKEY_HOST: {p.get('valkey_host', '')}
      VALKEY_PORT: "{p.get('valkey_port', 6379)}"
      VALKEY_SSL: "{'true' if p.get('ssl_enabled') else 'false'}"
      KEY_PATTERN: "{p.get('key_pattern', '{table}:{pk}')}"
      WRITE_MODE: {p.get('write_mode', 'hset')}
      BATCH_SIZE: "{p.get('pipeline_batch_size', 100)}"
      LWW_ENABLED: "{'true' if p.get('lww_enabled') else 'false'}"
      CLOUDWATCH_ENABLED: "true"
      ROW_FILTERS: '{p.get('transforms', '{}')}'
    volumes:
      - ./sink_worker.py:/app/sink_worker.py
    working_dir: /app
    command: bash -c "pip install kafka-python valkey boto3 && python -u sink_worker.py"
    deploy:
      replicas: {p.get('sink_replicas', 1)}
    restart: unless-stopped
COMPOSE

# Write connector config
cat > connector.json << 'CONNECTOR'
{{
  "name": "ecdl-{pipeline_name}-connector",
  "config": {{
    "connector.class": "io.debezium.connector.mysql.MySqlConnector",
    "database.hostname": "{p.get('source_host', '')}",
    "database.port": "{p.get('source_port', 3306)}",
    "database.user": "{p.get('db_user', '')}",
    "database.password": "{p.get('db_pass', '')}",
    "database.server.id": "{abs(hash(pipeline_name)) % 100000}",
    "topic.prefix": "ecdl.{pipeline_name}",
    "database.include.list": "{p.get('database_name', '')}",
    "table.include.list": "{p.get('connector_tables', '')}",
    "schema.history.internal.kafka.bootstrap.servers": "kafka:29092",
    "schema.history.internal.kafka.topic": "ecdl-schema-history-{pipeline_name}",
    "include.schema.changes": "false",
    "snapshot.mode": "{p.get('snapshot_mode', 'initial')}",
    "decimal.handling.mode": "double"
  }}
}}
CONNECTOR

# Copy sink worker code (download from S3 or embed)
cat > sink_worker.py << 'SINKEOF'
# Sink worker will be downloaded from the deployment package
import os, sys
print("Sink worker starting... downloading code")
SINKEOF

# Start the pipeline
docker-compose up -d

# Wait for Debezium
echo "Waiting for Debezium to be ready..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:8083/ > /dev/null 2>&1; then
        echo "Debezium ready!"
        break
    fi
    sleep 5
done

# Register connector
sleep 5
curl -s -X POST -H "Content-Type: application/json" --data @connector.json http://localhost:8083/connectors
echo ""
echo "=== ECDL Pipeline Deployed Successfully ==="
echo "Debezium: http://localhost:8083"
echo "Connector status: http://localhost:8083/connectors/ecdl-{pipeline_name}-connector/status"
'''

    # Launch EC2 instance
    launch_params = {
        'ImageId': _get_latest_ami(client),
        'InstanceType': instance_type,
        'MinCount': 1,
        'MaxCount': 1,
        'UserData': user_data,
        'TagSpecifications': [{
            'ResourceType': 'instance',
            'Tags': [
                {'Key': 'Name', 'Value': f'ecdl-pipeline-{pipeline_name}'},
                {'Key': 'Project', 'Value': 'ECDL'},
                {'Key': 'Pipeline', 'Value': pipeline_name},
            ]
        }],
        'IamInstanceProfile': {'Name': 'ecdl-ec2-ssm-profile'},
    }

    if subnet_id:
        launch_params['SubnetId'] = subnet_id
    if security_group_id:
        launch_params['SecurityGroupIds'] = [security_group_id]

    response = client.run_instances(**launch_params)
    instance_id = response['Instances'][0]['InstanceId']

    # Record deployment
    conn = get_db()
    conn.execute('''
        INSERT INTO deployments (pipeline_id, status, deploy_type, region, started, log)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (pipeline_id, 'provisioning', 'ec2', region,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
          f'Instance: {instance_id}\nType: {instance_type}'))
    conn.commit()
    conn.close()

    audit_log('deploy', 'pipeline', pipeline_id, pipeline_name,
              f'EC2 deploy: {instance_id} ({instance_type})')

    return jsonify({
        'success': True,
        'deployment_id': instance_id,
        'status': 'provisioning',
        'instance_id': instance_id,
        'instance_type': instance_type,
        'message': f'EC2 instance {instance_id} launching. Pipeline will auto-start via user-data. Check status with: aws ssm start-session --target {instance_id}'
    }), 201


def _deploy_to_existing_ec2(p, pipeline_name, config, region, pipeline_id, instance_id):
    """Deploy pipeline to an existing EC2 instance via SSM."""
    import boto3

    ssm = boto3.client('ssm', region_name=region)

    # Generate the deployment files and send via SSM
    transforms = p.get('transforms', '') or '{}'
    valkey_host = p.get('valkey_host', p.get('cluster_endpoint', ''))
    valkey_port = p.get('valkey_port', p.get('tgt_port', 6379))
    ssl_enabled = 'true' if p.get('ssl_enabled', p.get('tgt_ssl', True)) else 'false'
    db_host = p.get('source_host', p.get('host', ''))
    db_port = p.get('source_port', p.get('src_port', 3306))
    db_user = p.get('db_user', p.get('username', ''))
    db_pass = p.get('db_pass', p.get('password', ''))
    database_name = p.get('database_name', '')
    tables = p.get('connector_tables', p.get('tables', ''))
    snapshot_mode = p.get('snapshot_mode', 'initial')
    partitions = p.get('partitions', 3)
    batch_size = p.get('pipeline_batch_size', 100)
    key_pattern = p.get('key_pattern', '{table}:{pk}')
    write_mode = p.get('write_mode', 'hset')

    # Qualify table names for MySQL
    db_engine = p.get('source_engine', p.get('engine', 'mysql'))
    if db_engine == 'mysql' and tables:
        qualified = []
        for t in tables.split(','):
            t = t.strip()
            if t and '.' not in t:
                qualified.append(f"{database_name}.{t}")
            else:
                qualified.append(t)
        tables = ','.join(qualified)

    server_id = str(abs(hash(pipeline_name + instance_id)) % 100000 + 1)

    commands = [
        f'echo "=== Deploying pipeline {pipeline_name} to this EC2 ==="',
        'mkdir -p /home/ec2-user/ecdl-pipeline',
        'aws s3 cp s3://ecdl-deploy-111122223333/sink_worker.py /home/ec2-user/ecdl-pipeline/sink_worker.py',
        'aws s3 cp s3://ecdl-deploy-111122223333/sink_common.py /home/ec2-user/ecdl-pipeline/sink_common.py',
        # Stop existing pipeline containers
        'docker stop $(docker ps -q --filter name=ecdl-sink) 2>/dev/null; docker rm $(docker ps -aq --filter name=ecdl-sink) 2>/dev/null',
        'docker stop $(docker ps -q --filter name=ecdl-debezium) 2>/dev/null; docker rm $(docker ps -aq --filter name=ecdl-debezium) 2>/dev/null',
        # Ensure Kafka is running
        'docker ps | grep -q kafka || echo "WARNING: Kafka not running"',
        # Start Debezium
        f'docker run -d --name ecdl-debezium --network host --restart unless-stopped '
        f'-e BOOTSTRAP_SERVERS=localhost:9092 '
        f'-e GROUP_ID=ecdl-{pipeline_name} '
        f'-e CONFIG_STORAGE_TOPIC=ecdl_{pipeline_name}_configs '
        f'-e OFFSET_STORAGE_TOPIC=ecdl_{pipeline_name}_offsets '
        f'-e STATUS_STORAGE_TOPIC=ecdl_{pipeline_name}_status '
        f'-e CONFIG_STORAGE_REPLICATION_FACTOR=1 '
        f'-e OFFSET_STORAGE_REPLICATION_FACTOR=1 '
        f'-e STATUS_STORAGE_REPLICATION_FACTOR=1 '
        f'debezium/connect:2.5',
        'sleep 30',
        # Register connector
        f'curl -s -X POST http://localhost:8083/connectors -H "Content-Type: application/json" -d \'{{"name":"ecdl-{pipeline_name}-connector","config":{{"connector.class":"io.debezium.connector.mysql.MySqlConnector","database.hostname":"{db_host}","database.port":"{db_port}","database.user":"{db_user}","database.password":"{db_pass}","database.server.id":"{server_id}","topic.prefix":"ecdl.{pipeline_name}","database.include.list":"{database_name}","table.include.list":"{tables}","schema.history.internal.kafka.bootstrap.servers":"localhost:9092","schema.history.internal.kafka.topic":"ecdl-schema-{pipeline_name}","include.schema.changes":"false","snapshot.mode":"{snapshot_mode}","decimal.handling.mode":"double"}}}}\'',
        # Start sink worker
        f'docker run -d --name ecdl-sink --network host --restart unless-stopped '
        f'-e KAFKA_BOOTSTRAP_SERVERS=localhost:9092 '
        f'-e KAFKA_CONSUMER_GROUP=ecdl-sink-{pipeline_name} '
        f'-e KAFKA_TOPIC_PATTERN="ecdl.{pipeline_name}.*" '
        f'-e VALKEY_HOST={valkey_host} '
        f'-e VALKEY_PORT={valkey_port} '
        f'-e VALKEY_SSL={ssl_enabled} '
        f'-e KEY_PATTERN="{key_pattern}" '
        f'-e WRITE_MODE={write_mode} '
        f'-e BATCH_SIZE={batch_size} '
        f'-e LWW_ENABLED=false '
        f'-e CLOUDWATCH_ENABLED=true '
        f'-e CLOUDWATCH_NAMESPACE=ECDL/Pipeline '
        f'-e AWS_DEFAULT_REGION={region} '
        f"""-e 'ROW_FILTERS={transforms}' """
        f'-v /home/ec2-user/ecdl-pipeline/sink_worker.py:/app/sink_worker.py '
        f'-v /home/ec2-user/ecdl-pipeline/sink_common.py:/app/sink_common.py '
        f'public.ecr.aws/docker/library/python:3.11-slim bash -c "pip install kafka-python valkey boto3 && python -u /app/sink_worker.py"',
        f'echo "=== Pipeline {pipeline_name} deployed ==="',
    ]

    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        Parameters={'commands': commands},
        TimeoutSeconds=120
    )
    command_id = response['Command']['CommandId']

    # Record deployment
    conn = get_db()
    conn.execute('''
        INSERT INTO deployments (pipeline_id, status, deploy_type, region, started, log)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (pipeline_id, 'deploying', 'ec2', region,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
          f'Instance: {instance_id} (existing)\nCommand: {command_id}'))
    conn.commit()
    conn.close()

    audit_log('deploy', 'pipeline', pipeline_id, pipeline_name,
              f'Deployed to existing EC2: {instance_id}')

    return jsonify({
        'success': True,
        'deployment_id': command_id,
        'status': 'deploying',
        'instance_id': instance_id,
        'message': f'Pipeline deploying to existing EC2 {instance_id}. Debezium + sink worker starting.'
    }), 201


def _deploy_ecs(p, pipeline_name, config, region, pipeline_id):
    """Deploy pipeline to ECS Fargate with MSK."""
    # For now, return instructions — full ECS deploy is complex
    return jsonify({
        'success': True,
        'deployment_id': f'ecs-{pipeline_name}',
        'status': 'pending',
        'message': 'ECS Fargate deployment requires CloudFormation. Use "Download Package" to get the CloudFormation template, then deploy with: aws cloudformation create-stack --stack-name ecdl-pipeline --template-body file://cloudformation.json'
    }), 201


def _get_latest_ami(client):
    """Get the latest Amazon Linux 2023 AMI."""
    try:
        response = client.describe_images(
            Owners=['amazon'],
            Filters=[
                {'Name': 'name', 'Values': ['al2023-ami-2023*-x86_64']},
                {'Name': 'state', 'Values': ['available']},
            ]
        )
        images = sorted(response['Images'], key=lambda x: x['CreationDate'], reverse=True)
        return images[0]['ImageId'] if images else 'ami-0c55b159cbfafe1f0'
    except Exception:
        return 'ami-0c55b159cbfafe1f0'


# --- Pipeline Health Monitor (CloudWatch) ---
import threading
import requests as http_requests

_monitor_running = False

def _check_debezium(cw, dimensions):
    """Check Debezium connector status and push to CloudWatch."""
    try:
        resp = http_requests.get('http://localhost:8083/connectors', timeout=5)
        if resp.status_code != 200:
            cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
                {'MetricName': 'DebeziumReachable', 'Value': 0, 'Unit': 'None', 'Dimensions': dimensions}
            ])
            return
        cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
            {'MetricName': 'DebeziumReachable', 'Value': 1, 'Unit': 'None', 'Dimensions': dimensions}
        ])
        connectors = resp.json()
        cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
            {'MetricName': 'DebeziumConnectorCount', 'Value': len(connectors), 'Unit': 'Count', 'Dimensions': dimensions}
        ])
        failed_tasks = 0
        running_tasks = 0
        for name in connectors:
            try:
                s = http_requests.get(f'http://localhost:8083/connectors/{name}/status', timeout=5).json()
                conn_dims = [{'Name': 'Component', 'Value': 'Debezium'}, {'Name': 'Connector', 'Value': name}]
                conn_running = 1 if s.get('connector', {}).get('state') == 'RUNNING' else 0
                cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
                    {'MetricName': 'ConnectorRunning', 'Value': conn_running, 'Unit': 'None', 'Dimensions': conn_dims}
                ])
                for task in s.get('tasks', []):
                    if task.get('state') == 'RUNNING':
                        running_tasks += 1
                    else:
                        failed_tasks += 1
                        # Auto-restart failed tasks
                        task_id = task.get('id', 0)
                        try:
                            restart_resp = http_requests.post(
                                f'http://localhost:8083/connectors/{name}/tasks/{task_id}/restart', timeout=10
                            )
                            if restart_resp.status_code in (200, 204):
                                print(f"[HealthMonitor] Auto-restarted failed task {task_id} on connector {name}")
                            else:
                                print(f"[HealthMonitor] Failed to restart task {task_id} on {name}: HTTP {restart_resp.status_code}")
                        except Exception as re:
                            print(f"[HealthMonitor] Error restarting task {task_id} on {name}: {re}")
            except Exception:
                failed_tasks += 1
        cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
            {'MetricName': 'DebeziumRunningTasks', 'Value': running_tasks, 'Unit': 'Count', 'Dimensions': dimensions},
            {'MetricName': 'DebeziumFailedTasks', 'Value': failed_tasks, 'Unit': 'Count', 'Dimensions': dimensions},
            {'MetricName': 'TaskAutoRestarts', 'Value': failed_tasks, 'Unit': 'Count', 'Dimensions': dimensions},
        ])
    except Exception as e:
        try:
            cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
                {'MetricName': 'DebeziumReachable', 'Value': 0, 'Unit': 'None', 'Dimensions': dimensions}
            ])
        except Exception:
            pass


def _check_kafka(cw, dimensions):
    """Check all Kafka brokers from the brokers table and push to CloudWatch."""
    import socket
    brokers_checked = 0
    brokers_reachable = 0
    try:
        conn = get_db()
        brokers = conn.execute("SELECT name, bootstrap_servers FROM brokers WHERE broker_type='kafka' AND bootstrap_servers IS NOT NULL AND bootstrap_servers != ''").fetchall()
        conn.close()

        for broker in brokers:
            name = broker['name']
            servers = broker['bootstrap_servers']
            # Parse bootstrap_servers — could be "kafka:29092" or "host1:9092,host2:9092"
            for server in servers.split(','):
                server = server.strip()
                if not server:
                    continue
                parts = server.split(':')
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 9092
                brokers_checked += 1
                reachable = 0
                try:
                    sock = socket.create_connection((host, port), timeout=5)
                    sock.close()
                    reachable = 1
                    brokers_reachable += 1
                except Exception:
                    pass
                try:
                    broker_dims = [{'Name': 'Component', 'Value': 'Kafka'}, {'Name': 'Broker', 'Value': name}]
                    cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
                        {'MetricName': 'KafkaBrokerReachable', 'Value': reachable, 'Unit': 'None', 'Dimensions': broker_dims}
                    ])
                except Exception:
                    pass
    except Exception:
        pass

    # Also check the local Docker Kafka as fallback
    if brokers_checked == 0:
        reachable = 0
        try:
            sock = socket.create_connection(('kafka', 29092), timeout=5)
            sock.close()
            reachable = 1
        except Exception:
            pass
        try:
            cw.put_metric_data(Namespace='ECDL/Pipeline', MetricData=[
                {'MetricName': 'KafkaBrokerReachable', 'Value': reachable, 'Unit': 'None', 'Dimensions': dimensions}
            ])
        except Exception:
            pass


def _pipeline_health_monitor():
    """Background thread that monitors Debezium and Kafka, pushes to CloudWatch."""
    global _monitor_running
    try:
        cw = boto3.client('cloudwatch')
        _monitor_running = True
        print("[HealthMonitor] Started — publishing Debezium & Kafka metrics to CloudWatch every 60s")
    except Exception as e:
        print(f"[HealthMonitor] CloudWatch unavailable: {e}")
        return

    dimensions = [{'Name': 'Component', 'Value': 'Infrastructure'}]

    while True:
        try:
            _check_debezium(cw, dimensions)
            _check_kafka(cw, dimensions)
        except Exception as e:
            print(f"[HealthMonitor] Error: {e}")
        time.sleep(60)


def start_health_monitor():
    """Start the pipeline health monitor in a daemon thread."""
    t = threading.Thread(target=_pipeline_health_monitor, daemon=True)
    t.start()


if __name__ == '__main__':
    init_db()
    start_health_monitor()
    # Loopback unless told otherwise. The container image sets ECDL_BIND_HOST to
    # all interfaces because a container must accept its published port; the
    # publish (and the security group in front of it) is what limits who reaches
    # it. A bare `python app.py` on a laptop or a server stays private.
    app.run(host=os.environ.get('ECDL_BIND_HOST', '127.0.0.1'), port=5000, debug=False)
