"""Control-plane authentication.

WHY THIS EXISTS NOW. The control plane had no authentication of any kind -- 80
route-methods, 39 of them mutating, zero checks. That was an acceptable risk while it
could only read: the worst case was someone seeing a binlog position. It stopped
being acceptable when the instance role was granted deploy permissions, because
`POST /api/links/deploy` then creates CloudFormation stacks, ECS services, IAM
roles and ElastiCache clusters. Anyone who could reach the port could spend money
and create IAM roles.

WHAT THIS IS, AND WHAT IT IS NOT. It is a single shared bearer token, checked in
constant time, required on every request that changes something. It is NOT user
management: there are no accounts, no roles and no audit trail of *who* acted --
only that the caller held the token. For an internal control plane behind a
restricted security group that is a proportionate control, and it is much
safer than having no control. If per-user identity is needed, this is the component to replace
with an ALB and an OIDC provider, and the decorator is the only thing that changes.

FAIL CLOSED. If no token is configured, mutating requests are REFUSED rather than
allowed. The opposite ("no token, so skip the check") is how this bug usually
appears: it makes a misconfiguration look like a working system, and the system it
looks like is the unprotected one. Running open has to be asked for explicitly,
and it says so on every start.

READS ARE OPEN BY DEFAULT, deliberately. The read surface is dashboards -- table
names, stream depths, CloudWatch numbers. It holds no credentials (a separate test
asserts none reach /health), and the monitor is meant to be watchable on a wall
screen. The risk that motivated this work is mutation. Set ECDL_AUTH_READS=true to
require the token on GETs too.

CONFIGURATION
  ECDL_API_TOKEN            the token itself
  ECDL_API_TOKEN_SECRET_ID  else a Secrets Manager id/ARN holding it
  ECDL_AUTH_DISABLED=true   run with no authentication at all (prints a warning)
  ECDL_AUTH_READS=true      require the token on GET/HEAD as well
  ECDL_AUTH_REGION          region for the secret lookup (default us-east-2)

The token is never placed in a config dict, never logged, and never returned by
any endpoint. /health echoes its configuration, so anything kept there is
published -- that is exactly why db_password() and broker_password() are functions
rather than entries, and this follows them.
"""

import functools
import hmac
import os

from flask import jsonify, request

# Methods that change something. Everything here needs the token.
WRITE_METHODS = ('POST', 'PUT', 'DELETE', 'PATCH')

# CORS preflight carries no Authorization header by design -- the browser sends it
# before it will send the real request. Blocking it breaks every cross-origin call
# in a way that looks like a network fault rather than an auth failure.
EXEMPT_METHODS = ('OPTIONS',)

# ---------------------------------------------------------------------------
# Browser request checks
#
# The API is often reachable without a login: in AccessMode=ssm it is
# http://localhost:5000 on the operator's own machine for as long as the SSM
# tunnel is open. Without these checks any web page that operator visits could
# send a deploy or delete request to it from their browser.
#
#   ECDL_ALLOWED_ORIGINS  comma-separated web origins (scheme://host:port) that
#                         may call the API from a browser, besides the API's own
#                         origin. Unset: the local UI addresses below. Set to an
#                         empty value: same-origin only (behind a load balancer).
#   ECDL_ALLOWED_HOSTS    comma-separated Host header values the API answers.
#                         Unset or empty: any. Set in ssm mode, so a web page on
#                         another domain that resolves to 127.0.0.1 (DNS
#                         rebinding) is refused.
#
# Requests with no Origin header (curl, the CLI, health checks) are not browser
# cross-site requests and are not affected; whoever sends them already reached
# the port through IAM, the load balancer or the allowed CIDR.
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_ORIGINS = (
    'http://localhost:3000', 'http://127.0.0.1:3000',    # SSM tunnel
    'http://localhost:3001', 'http://127.0.0.1:3001',    # docker compose (local)
)


def _csv_env(name):
    """None when the variable is unset; otherwise its non-empty items."""
    value = os.getenv(name)
    if value is None:
        return None
    return tuple(item.strip().rstrip('/') for item in value.split(',') if item.strip())


def allowed_origins():
    configured = _csv_env('ECDL_ALLOWED_ORIGINS')
    return DEFAULT_ALLOWED_ORIGINS if configured is None else configured


def allowed_hosts():
    return tuple(h.lower() for h in (_csv_env('ECDL_ALLOWED_HOSTS') or ()))


def _origin_host(origin):
    """The host:port part of an Origin header value, lower-cased."""
    rest = origin.split('://', 1)[1] if '://' in origin else origin
    return rest.split('/', 1)[0].lower()


def _hostname(host_port):
    """The host part of host[:port], without IPv6 brackets."""
    if host_port.startswith('['):
        return host_port[1:].split(']', 1)[0]
    return host_port.rsplit(':', 1)[0] if host_port.count(':') == 1 else host_port


def _is_ip_or_localhost(name):
    if name == 'localhost':
        return True
    import ipaddress
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _forbidden(message, hint):
    return jsonify({'error': message, 'hint': hint, 'authenticated': False}), 403


def check_browser_request():
    """Refuse a request for an unexpected Host, or a write from another site.

    Returns None to allow, or a 403 response.
    """
    hosts = allowed_hosts()
    host = (request.headers.get('Host') or '').lower()
    if hosts and host not in hosts:
        return _forbidden(
            'requests for host {!r} are not accepted'.format(host),
            'Open the UI at one of: {}.'.format(', '.join(hosts)))
    if request.method not in WRITE_METHODS:
        return None
    origin = (request.headers.get('Origin') or '').strip().rstrip('/')
    if not origin:
        return None
    if origin in allowed_origins():
        return None
    if origin != 'null':
        origin_host = _origin_host(origin)
        # Same origin: the page was served from the address this request was
        # sent to (the load-balancer setup, where /api/* and the UI share one
        # domain).
        if origin_host == host:
            return None
        # The UI and API on one machine, different ports (UI :3000, API :5000),
        # addressed by IP or localhost. A domain name is not accepted here,
        # because a hostile domain can be made to resolve to this address.
        page, api = _hostname(origin_host), _hostname(host)
        if page == api and _is_ip_or_localhost(api):
            return None
    return _forbidden(
        'write requests from web origin {!r} are not accepted'.format(origin),
        'Changes can only be made from the ECDL UI itself. Allowed origins are '
        'set with ECDL_ALLOWED_ORIGINS.')

_token_cache = {}


def _env(name, default=''):
    value = os.getenv(name)
    return default if value is None else value


def _truthy(name):
    return _env(name).strip().lower() in ('1', 'true', 'yes', 'on')


def auth_disabled():
    return _truthy('ECDL_AUTH_DISABLED')


def protect_reads():
    return _truthy('ECDL_AUTH_READS')


def configured_token():
    """The expected token, from the environment or Secrets Manager.

    Cached, because this is consulted on every request and a Secrets Manager call
    per request would be both slow and a quota problem. Returns '' when nothing is
    configured, which the caller treats as "refuse writes", not as "allow".
    """
    direct = _env('ECDL_API_TOKEN').strip()
    if direct:
        return direct

    secret_id = _env('ECDL_API_TOKEN_SECRET_ID').strip()
    if not secret_id:
        return ''
    if secret_id in _token_cache:
        return _token_cache[secret_id]
    try:
        import boto3
        import json as _json
        client = boto3.client('secretsmanager',
                              region_name=_env('ECDL_AUTH_REGION', 'us-east-2'))
        raw = client.get_secret_value(SecretId=secret_id)['SecretString']
        try:                              # a generated secret may be JSON or plain
            value = _json.loads(raw).get('token') or _json.loads(raw).get('password') or ''
        except (ValueError, AttributeError):
            value = raw
        value = (value or '').strip()
    except Exception as exc:                              # noqa: BLE001
        # Deliberately NOT cached: a transient Secrets Manager failure must not
        # keep an empty token cached for the life of the process, which would make the
        # control plane refuse its own writes until someone restarted it.
        print('[auth] could not read {}: {}'.format(secret_id, exc))
        return ''
    _token_cache[secret_id] = value
    return value


def reset_cache():
    """Drop the cached token. For tests, and after a rotation."""
    _token_cache.clear()


def presented_token():
    """The token the caller sent.

    `Authorization: Bearer <token>` is the primary form. X-ECDL-Token is accepted
    because curl and shell scripts commonly use it, and refusing it would lead people
    to put the token in a query string, where it would be recorded in access
    logs.
    """
    header = request.headers.get('Authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return (request.headers.get('X-ECDL-Token') or '').strip()


def token_ok():
    expected = configured_token()
    if not expected:
        return False
    # Constant time: a plain == leaks the length of the matching prefix through
    # timing, which is enough to recover a token given enough attempts.
    return hmac.compare_digest(presented_token(), expected)


def _refuse_unconfigured():
    return jsonify({
        'error': 'this control plane has no API token configured, so it refuses '
                 'requests that would change anything',
        'hint': 'Set ECDL_API_TOKEN, or ECDL_API_TOKEN_SECRET_ID pointing at a '
                'Secrets Manager secret. To run with no authentication at all '
                'set ECDL_AUTH_DISABLED=true -- only do that on an isolated '
                'network, because this control plane can create AWS '
                'infrastructure.',
        'authenticated': False,
    }), 503


def _refuse_unauthorised():
    response = jsonify({
        'error': 'a valid API token is required for this request',
        'hint': 'Send it as "Authorization: Bearer <token>".',
        'authenticated': False,
    })
    # So a client knows what kind of credential to present rather than guessing.
    response.headers['WWW-Authenticate'] = 'Bearer realm="ecdl"'
    return response, 401


def check_request():
    """The decision for one request. Returns None to allow, or a response.

    Kept separate from the hook so it can be tested directly and reused by a
    decorator, and so the ordering of the three cases is visible in one place.
    """
    if request.method in EXEMPT_METHODS:
        return None
    # Before the token decision, and regardless of it: when there is no login,
    # these checks are what stop a web page from sending a deploy request.
    refused = check_browser_request()
    if refused is not None:
        return refused
    if auth_disabled():
        return None
    needs_token = request.method in WRITE_METHODS or protect_reads()
    if not needs_token:
        return None
    if not configured_token():
        return _refuse_unconfigured()
    if not token_ok():
        return _refuse_unauthorised()
    return None


def install(app):
    """Protect every route on this app, including ones added later.

    A before_request hook rather than a decorator on each view: there are 80
    route-methods across three modules, and any scheme that requires remembering
    to annotate a new one will eventually miss the one that matters. This way a
    route added tomorrow is protected without anybody deciding to protect it.
    """
    @app.before_request
    def _require_token():                                 # noqa: ANN202
        return check_request()

    mode = ('DISABLED' if auth_disabled()
            else ('all requests' if protect_reads() else 'writes only'))
    configured = bool(configured_token())
    if auth_disabled():
        print('[auth] *** AUTHENTICATION DISABLED *** every endpoint is open, '
              'including deploy. This control plane can create AWS '
              'infrastructure -- only run this way on an isolated network.')
    elif not configured:
        print('[auth] no API token configured: read endpoints work, and anything '
              'that changes state will be refused with 503 until '
              'ECDL_API_TOKEN or ECDL_API_TOKEN_SECRET_ID is set.')
    else:
        print('[auth] API token required for {}.'.format(mode))
    return app


def require_token(view):
    """Decorator form, for a route that must be protected even when reads are open.

    Used on the endpoints that look like reads but are expensive or state-changing in
    practice, where the method alone does not capture the risk.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if auth_disabled():
            return view(*args, **kwargs)
        if not configured_token():
            return _refuse_unconfigured()
        if not token_ok():
            return _refuse_unauthorised()
        return view(*args, **kwargs)
    return wrapper


def status():
    """Whether authentication is on, for the UI to adapt to.

    Reports only whether a token is REQUIRED and whether one is CONFIGURED. It
    never returns the token, and it is what lets the page ask for a token
    before the first request instead of after the first 401.
    """
    return {
        'enabled': not auth_disabled(),
        'configured': bool(configured_token()),
        'protects': ('nothing' if auth_disabled()
                     else ('all requests' if protect_reads() else 'writes')),
        'scheme': 'bearer',
    }
