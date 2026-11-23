from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import sqlite3
import os
import tempfile
import io
from datetime import datetime

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


DATABASE_PATH = os.getenv('DATABASE_PATH', '/app/data/app.db')


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
# The superseded object model (review finding H10)
# ============================================================
# Data sources, connectors, brokers, targets, pipelines and their deployments
# described a pipeline as five separate records. A link replaces all of them,
# and they deployed nothing. They were removed with their pages rather than
# kept: the data-source endpoints connected to any host a user had typed in,
# with a secret the instance role read (scan-tables, tables, columns), and the
# Randomizer wrote generated rows into those databases. Their tables stay in
# the database so an older backup still restores (Admin -> Maintenance).

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


import repo_path  # noqa: E402,F401  (puts ecdl_link on sys.path)


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
    # The sampled cache check of each link (review finding C1), started only
    # in the server process, never when tests import this module.
    import verify_schedule                                   # noqa: E402
    import links as _links_mod                               # noqa: E402
    verify_schedule.start(busy=lambda link: bool(_links_mod._RUNNING_LINKS.get(link)))
    # Loopback unless told otherwise. The container image sets ECDL_BIND_HOST to
    # all interfaces because a container must accept its published port; the
    # publish (and the security group in front of it) is what limits who reaches
    # it. A bare `python app.py` on a laptop or a server stays private.
    app.run(host=os.environ.get('ECDL_BIND_HOST', '127.0.0.1'), port=5000, debug=False)
