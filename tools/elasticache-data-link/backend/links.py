"""Link configuration endpoints (/api/links/*).

Thin wrapper over `ecdl_link`, which is the shared definition the CLI uses too.
This module deliberately contains no validation logic of its own -- the moment
the API validates differently from the file, the two surfaces have drifted, which
is the problem this endpoint exists to prevent.

    GET  /api/links/schema      the schema, so the UI can render its form from it
    POST /api/links/validate    verdict + normalised document for a candidate config
    POST /api/links/resolve     what the ARNs point at (read-only)
    POST /api/links/plan        what would be deployed, and in what order (read-only)
    POST /api/links/deploy      do it, in the background
    GET  /api/links/deployments/<id>   progress of a running or finished deploy
"""

import json
import logging
import re
import os

import yaml
from flask import Blueprint, jsonify, request

# `ecdl_link` lives at the repo root so the CLI and the API can share it.
import repo_path  # noqa: E402,F401  (puts ecdl_link on sys.path)

_HERE = os.path.dirname(os.path.abspath(__file__))

from ecdl_link import (  # noqa: E402
    LinkValidationError,
    canonical_json,
    normalize,
    problems_for,
    schema as link_schema,
    to_yaml,
)
from ecdl_link.resolve import (  # noqa: E402
    ResolutionError,
    describe_resolution,
    resolve_link,
)
from ecdl_link.provision import (  # noqa: E402
    PlanError, deployed_pg_publication, describe_plan, plan as build_plan,
)
from ecdl_link.repair import (  # noqa: E402
    RepairError,
    describe_diff,
    describe_repair,
    diff,
    repair_plan,
)
from ecdl_link.deploy import (  # noqa: E402
    Clients as DeployClients,
    DeployError,
    Deployer,
    add_load_step,
    broker_offset_probe,
    LOAD_INCOMPLETE,
    deployment_state,
    initial_load_refusal,
    load_marker,
    reset_position_refusal,
    plan_pauses_capture,
    wants_update_plan,
)
from ecdl_link.change import (  # noqa: E402
    describe_review, follow_up, review_deployed,
)
from ecdl_link.preflight import (  # noqa: E402
    RETENTION_PATH, pipeline_secret_list, preflight as preflight_source, secret_findings,
    target_findings, without_path,
)
from ecdl_link.teardown import (  # noqa: E402
    Destroyer, describe_broker_data, describe_destroy_plan, destroy_plan,
)
from ecdl_link.conflicts import (  # noqa: E402
    CONFLICT_HINT, describe_conflict, resolution_conflicts,
)
from ecdl_link.discover import (  # noqa: E402
    DEFAULT_LIMIT as DISCOVER_LIMIT,
    control_plane_security_group,
    discover,
    secrets_for_source,
)
from ecdl_link.tables import list_tables  # noqa: E402

# Discovery is read-shaped but enumerates the account, so it is protected even
# when ordinary reads are open. Imported defensively: `links.py` is registered in
# a try/except by app.py so the rest of the control plane survives without it,
# and a hard import failure here would stop the whole blueprint from loading.
try:
    from auth import require_token
except ImportError:  # pragma: no cover - only when auth.py is absent
    def require_token(view):
        return view

links_bp = Blueprint('links', __name__, url_prefix='/api/links')


@links_bp.route('/schema', methods=['GET'])
def get_schema():
    """The link schema.

    Served rather than duplicated in the frontend so a field cannot exist in the
    form without existing in the schema, which is what keeps the UI and the file
    consistent about accepting the same things.
    """
    return jsonify(link_schema())


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases (*name, and the <<: *name merge).

    A few hundred bytes of nested aliases expand to millions of nodes (the
    "billion laughs" document), which safe_load builds in memory and the
    validator then walks. A link config has no use for them (Jira story F159).
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, 'aliases (*{}) are not accepted in a link config'.format(
                    event.anchor), event.start_mark)
        return super().compose_node(parent, index)


@links_bp.route('/parse', methods=['POST'])
def parse_endpoint():
    """Turn the text of a link file into a document, and validate it.

    The UI has to accept the SAME file the CLI accepts, and that file is usually
    YAML. The alternative was a YAML parser in the browser, which would mean two
    parsers that can disagree about the same bytes -- exactly the split between
    file and form this design is meant to remove. So the text is parsed here, by
    the same PyYAML the CLI uses, and JSON is also accepted because YAML is a
    superset of it.

    Validation comes back in the same response purely so a text editor needs one
    round trip per keystroke-batch rather than two.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('text'), str):
        return jsonify({
            'error': 'send {"text": "<contents of the link file>"}',
        }), 400

    text = body['text']
    if not text.strip():
        return jsonify({'parsed': False, 'error': 'the config is empty'})

    try:
        document = yaml.load(text, Loader=_NoAliasLoader)  # nosec B506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        # Report the position when the parser knows it, so the editor can say
        # WHERE rather than just that something is wrong.
        mark = getattr(exc, 'problem_mark', None)
        return jsonify({
            'parsed': False,
            'error': 'could not parse the config: {}'.format(
                getattr(exc, 'problem', None) or exc),
            'line': (mark.line + 1) if mark else None,
            'column': (mark.column + 1) if mark else None,
        })

    if not isinstance(document, dict):
        return jsonify({
            'parsed': False,
            'error': 'a link config must be a mapping at the top level, not {}'
                     .format(type(document).__name__),
        })

    found = problems_for(document)
    payload = {
        'parsed': True,
        'document': document,
        'valid': not found,
        'problems': [{'path': p.path, 'message': p.message} for p in found],
    }
    if not found:
        payload['normalized'] = normalize(document)
    return jsonify(payload)


@links_bp.route('/validate', methods=['POST'])
def validate():
    """Validate a candidate link config.

    Returns 200 with a verdict rather than 400 on an invalid document: the
    request itself is well formed -- it is asking whether the payload is valid --
    and a form needs the full problem list to annotate its fields, not an error
    status. A malformed body (not JSON, not an object) is a genuine 400.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({
            'error': 'send the link config as a JSON object in the request body',
        }), 400
    if not isinstance(body, dict):
        return jsonify({
            'error': 'expected a JSON object at the top level',
        }), 400

    # The UI may wrap the document, e.g. {"link": {...}}. Accept either shape so
    # the same payload works from a form post and from `curl -d @my-link.json`.
    document = body.get('link') if isinstance(body.get('link'), dict) else body

    found = problems_for(document)
    payload = {
        'valid': not found,
        'problems': [{'path': p.path, 'message': p.message} for p in found],
    }
    if not found:
        normalised = normalize(document)
        payload['normalized'] = normalised
        payload['canonical'] = canonical_json(normalised)
    return jsonify(payload)


@links_bp.route('/resolve', methods=['POST'])
def resolve():
    """Read everything derivable from the config's ARNs and report it back.

    This is the review step before deploying: it shows the customer what was
    discovered -- endpoints, engine, cluster mode, TLS, auth mode, the stream and
    key names each table will use, and which components the chosen mode creates --
    so nothing about the deployment is a surprise.

    Read-only, and it never fetches a secret value: the username comes from RDS
    and the auth mode from the replication group, so a `DescribeSecret` is enough
    to confirm the credential is reachable.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({
            'error': 'send the link config as a JSON object in the request body',
        }), 400
    document = body.get('link') if isinstance(body.get('link'), dict) else body

    found = problems_for(document)
    if found:
        # Resolving an invalid config would report confusing AWS errors caused by
        # a config problem we can already name precisely.
        return jsonify({
            'resolved': False,
            'stage': 'validate',
            'problems': [{'path': p.path, 'message': p.message} for p in found],
        }), 200

    try:
        resolution = resolve_link(document, region=body.get('region'))
    except ResolutionError as exc:
        return jsonify({
            'resolved': False,
            'stage': 'resolve',
            'error': str(exc),
            'hint': exc.hint,
        }), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            'resolved': False,
            'stage': 'resolve',
            'error': 'unexpected failure reading the referenced resources: '
                     '{}'.format(exc),
        }), 500

    # `link` is the caller's own document echoed back; dropping it keeps the
    # response about what was discovered.
    resolution.pop('link', None)
    return jsonify({
        'resolved': True,
        'resolution': resolution,
        'summary': describe_resolution(resolution),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Provisioning
#
# Before this, the control plane could not deploy this architecture at all.
# `_deploy_ecs` in app.py answered "ECS Fargate deployment requires
# CloudFormation. Use Download Package", and the package it generated described a
# DIFFERENT architecture (Debezium Connect + Kafka/MSK + a Python sink) as a shell
# script for someone to run manually. So the thing every benchmark measures was the
# one thing the product could not deploy.
# ═══════════════════════════════════════════════════════════════════════════

import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

_INFRA_DIR = None
for _candidate in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_candidate, 'infra')):
        _INFRA_DIR = os.path.join(_candidate, 'infra')
        break


def _deploy_settings():
    """TemplateURL base and CloudFormation service role (decision D1).

    Set by the control-plane stack: ECDL_TEMPLATE_URL_BASE is the release's
    copy of the templates in S3 (https://<bucket>.s3.<region>.amazonaws.com/
    templates/<version>/) and ECDL_DEPLOY_ROLE_ARN the role CloudFormation uses
    for every link stack. The instance itself may only pass that role and only
    with those templates, so neither can be left out: without both, deploys
    are refused rather than tried with permissions the instance does not have.
    """
    base = (os.environ.get('ECDL_TEMPLATE_URL_BASE') or '').strip()
    role = (os.environ.get('ECDL_DEPLOY_ROLE_ARN') or '').strip()
    if not base and not role:
        return {}
    if not (base and role):
        raise DeployError('ECDL_TEMPLATE_URL_BASE and ECDL_DEPLOY_ROLE_ARN must be set together')
    if not base.endswith('/'):
        base += '/'

    def url(filename):
        if os.path.basename(filename) != filename:
            raise DeployError('template {!r} is not a bare file name'.format(filename))
        return base + filename
    return {'template_url': url, 'role_arn': role}


def _template_body(filename):
    """The template text, read from infra/.

    Read at deploy time rather than built into the image so a template fix does not need a
    control-plane release. The name is checked against a basename to keep a
    crafted plan from reading an arbitrary file.
    """
    if _INFRA_DIR is None:
        raise DeployError(
            'the control plane cannot find its CloudFormation templates',
            'infra/ must be present next to backend/ or one level up.')
    if os.path.basename(filename) != filename:
        raise DeployError('template {!r} is not a bare file name'.format(filename))
    path = os.path.join(_INFRA_DIR, filename)
    if not os.path.isfile(path):
        raise DeployError('template {} not found in {}'.format(filename, _INFRA_DIR))
    with open(path) as handle:
        return handle.read()


# Deployment records. A deploy takes minutes (ElastiCache alone), so it cannot
# happen inside the request; it runs on a background thread and is polled.
#
# The records are PERSISTED, to a JSON file written atomically (temp file +
# rename, so a crash mid-write leaves the previous version, never half a file).
# They used to be memory-only, on the reasoning that the stacks are the durable
# truth -- which is right for "what exists", and wrong for "what was this deploy
# doing when it stopped". A restart in the middle of a load lost the one fact that
# matters most: capture had been PAUSED and nothing was going to resume it. On
# startup, a record still marked running is now shown as `interrupted`, with
# whether capture was left paused and the exact command to resume it.
#
# The stacks remain the authority on what exists: these records are a log, and
# nothing decides what to deploy from them.
_DEPLOYMENTS = {}
_DEPLOY_LOCK = threading.Lock()
# Links with a deploy running in THIS process. CloudFormation's *_IN_PROGRESS
# states guard against another process; this guards the window before the first
# stack call, where two requests for the same link would otherwise both start.
_RUNNING_LINKS = {}
_STATE_PATH = os.environ.get(
    'ECDL_DEPLOY_STATE_PATH', os.path.join(_HERE, 'data', 'deployments.json'))
_KEEP_RECORDS = 50


_LOG = logging.getLogger('ecdl.links')
# The last persist error, so a full disk is logged once rather than on every
# event, and its recovery is logged too.
_PERSIST_ERROR = [None]


def _persist_locked():
    """Write every record to disk. Caller holds _DEPLOY_LOCK. Never raises.

    A failure does not fail the deploy, but it is logged at ERROR (Jira story F103): it
    used to be swallowed, and a record that is not on disk is the one that
    says capture was left paused after a restart.
    """
    if not _STATE_PATH:
        return
    try:
        records = sorted(_DEPLOYMENTS.values(),
                         key=lambda r: r.get('started_at') or 0)[-_KEEP_RECORDS:]
        os.makedirs(os.path.dirname(_STATE_PATH) or '.', exist_ok=True)
        tmp = '{}.tmp'.format(_STATE_PATH)
        with open(tmp, 'w') as handle:
            json.dump(records, handle, default=str)
        os.replace(tmp, _STATE_PATH)
    except Exception as exc:  # noqa: BLE001 -- a full disk must not fail a deploy
        if str(exc) != _PERSIST_ERROR[0]:
            _PERSIST_ERROR[0] = str(exc)
            _LOG.error('could not save the deployment records to %s: %s. A restart '
                       'now loses what running deploys were doing, including '
                       'whether capture is paused.', _STATE_PATH, exc)
        return
    if _PERSIST_ERROR[0] is not None:
        _PERSIST_ERROR[0] = None
        _LOG.error('deployment records are being saved to %s again', _STATE_PATH)


# ── audit (Jira story F163) ─────────────────────────────────────────────────────────
# One JSON line per deploy, destroy or repair request and per finished run,
# on stdout, which the container log ships to CloudWatch. There is no user
# identity here (the API holds one shared token; decision D2 leaves identity
# with the customer's front end), so "who" is where the request came from and
# any request id the front end sent, which lets its own log say who it was.
# Never the Authorization header or a request body.
_AUDIT_PREFIX = '[audit] '


def _request_source():
    """Where the current request came from, or {} outside a request."""
    try:
        from flask import has_request_context
        if not has_request_context():
            return {}
        headers = request.headers
        out = {
            'remote_addr': request.remote_addr,
            'forwarded_for': (headers.get('X-Forwarded-For') or '').split(',')[0].strip()[:64]
                             or None,
            'user_agent': (headers.get('User-Agent') or '')[:200] or None,
            'request_id': (headers.get('X-Request-Id') or headers.get('X-Amzn-Trace-Id')
                           or '')[:128] or None,
            'method': request.method,
            'path': request.path,
        }
        return {k: v for k, v in out.items() if v}
    except Exception:  # noqa: BLE001 -- the audit line must never fail a request
        return {}


def _audit(action, link, result, source=None, **fields):
    """Write one audit line: who (source), what (action, link), when, result."""
    entry = {'audit': 'ecdl', 'action': action, 'link': link, 'result': result,
             'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    entry['source'] = source if source is not None else _request_source()
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        print(_AUDIT_PREFIX + json.dumps(entry, sort_keys=True, default=str), flush=True)
    except Exception:  # noqa: BLE001  # nosec B110 - a closed stdout must not fail the request
        pass


def _audit_response(action, payload, status, link=None, source=None):
    """The audit line for a deploy/destroy/repair response."""
    payload = payload or {}
    accepted = status == 202
    _audit(action, payload.get('link') or link,
           'accepted' if accepted else 'refused', source=source, status=status,
           deployment_id=payload.get('deployment_id') or payload.get('destroy_id'),
           stage=None if accepted else payload.get('stage'),
           reason=None if accepted else (payload.get('error') or payload.get('message')))


# Steps that pause and resume capture. reset_*: the brief resume and second
# pause of a position reset (Jira story F057).
_PAUSE_STEPS = ('pause_capture', 'reset_pause_capture')
_RESUME_STEPS = ('resume_capture', 'reset_resume_capture')
# Events after which capture is not paused by the deploy any more.
_RESUMED_EVENTS = ('capture_resumed_after_failure', 'capture_resume_unconfirmed')


def capture_pause_transition(event):
    """'pausing', 'paused' or 'resumed' when an event changes capture, else None.

    A pause that has STARTED counts as 'pausing' (Jira story F085): the desired count
    may already be 0 when the process stops before the pause completes."""
    name, step = event.get('event'), event.get('step')
    if step in _PAUSE_STEPS and name == 'action_started':
        return 'pausing'
    if step in _PAUSE_STEPS and name == 'action_complete':
        return 'paused'
    if name in _RESUMED_EVENTS or (name == 'action_complete' and step in _RESUME_STEPS):
        return 'resumed'
    return None


def capture_pause_state(record):
    """'paused', 'pausing' (possibly paused) or None for a deployment record.

    Read from the record's own capture_pause field (Jira story F103): the event log keeps
    the last 500 events, so on a long load the pause event fell off the front
    and a record of a deploy that left capture paused said "Deploy again".
    Records written before the field existed are read from their events."""
    if 'capture_pause' in record:
        return record.get('capture_pause') if record.get('capture_pause') in (
            'pausing', 'paused') else None
    state = None
    for event in record.get('events') or []:
        change = capture_pause_transition(event)
        if change is not None:
            state = None if change == 'resumed' else change
    return state


def capture_left_paused(record):
    """True if a record shows capture paused (or pausing) and never resumed."""
    return capture_pause_state(record) is not None


def _restore_deployments():
    """Reload persisted records; anything still `running` was interrupted."""
    if not _STATE_PATH or not os.path.exists(_STATE_PATH):
        return 0
    try:
        with open(_STATE_PATH) as handle:
            records = json.load(handle)
    except Exception:  # noqa: BLE001
        return 0
    with _DEPLOY_LOCK:
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or not record.get('id'):
                continue
            if record.get('status') == 'running' and record.get('kind') == 'destroy':
                record['status'] = 'interrupted'
                record['error'] = ('the control plane restarted while this delete '
                                   'was running, so it may not have finished')
                record['hint'] = ('Delete again: stacks already deleted are '
                                  'skipped, the rest are deleted.')
            elif record.get('status') == 'running':
                record['status'] = 'interrupted'
                record['error'] = ('the control plane restarted while this deploy '
                                   'was running, so it did not finish')
                pause_state = capture_pause_state(record)
                if pause_state:
                    record['capture_paused'] = True
                    lead = ('CAPTURE WAS LEFT PAUSED.' if pause_state == 'paused' else
                            'CAPTURE MAY HAVE BEEN LEFT PAUSED: the restart came while '
                            'it was being paused, so check the Debezium service\'s '
                            'desired count (aws ecs describe-services --cluster {c} '
                            '--services {s}).').format(
                        c=record.get('cluster_name') or '<cluster>',
                        s=record.get('debezium_service') or '<debezium service>')
                    record['hint'] = lead + (
                        ' Confirm the load task has '
                        'stopped (aws ecs list-tasks --cluster {c}), then resume '
                        'capture: aws ecs update-service --cluster {c} --service '
                        '{s} --desired-count 1. Then redeploy with a reload to '
                        'finish the load.'.format(
                            c=record.get('cluster_name') or '<cluster>',
                            s=record.get('debezium_service') or '<debezium service>'))
                else:
                    record['hint'] = ('Deploy again. Stacks that finished are '
                                      'updated in place; the rest are created.')
            _DEPLOYMENTS[record['id']] = record
        _persist_locked()
    return len(records) if isinstance(records, list) else 0


_restore_deployments()

# Terminal stack states that mean "already deployed and healthy". A stack in one
# of these is serving, so redeploying updates it in place rather than making a
# second copy -- which is the thing the deploy guard warns about. CREATE_FAILED
# and the rollback states are deliberately absent: those are retries, not
# redeploys, and the deployer deletes and recreates them itself.
_DEPLOYED_STATES = ('CREATE_COMPLETE', 'UPDATE_COMPLETE',
                    'UPDATE_ROLLBACK_COMPLETE', 'IMPORT_COMPLETE')


def existing_deployment(plan_doc, clients):
    """Which of this link's stacks already exist in a healthy state.

    Returns {stack_name: status} for the finished, usable stacks, so the deploy
    endpoint can tell "already deployed" from "never deployed" and from "a failed
    attempt to clean up". An empty dict means there is nothing to warn about.

    Reads CloudFormation directly rather than the in-memory deployment records:
    the records vanish on restart and a link may have been deployed by the CLI,
    so the stacks are the only durable truth about what exists.
    """
    names = [step['name'] for step in plan_doc.get('steps', []) if step.get('name')]
    found = {}
    for name in names:
        try:
            described = clients.cloudformation.describe_stacks(StackName=name)
        except Exception:                                 # noqa: BLE001
            # "does not exist" is the normal not-deployed case; anything else we
            # leave out rather than block a deploy on a transient describe error.
            continue
        stacks = described.get('Stacks') or []
        if not stacks:
            continue
        status = stacks[0].get('StackStatus')
        if status in _DEPLOYED_STATES:
            found[name] = status
    return found


def _record(deployment_id, **fields):
    with _DEPLOY_LOCK:
        entry = _DEPLOYMENTS.setdefault(deployment_id, {'events': []})
        entry.update(fields)
        _persist_locked()


def _stamp(event):
    """Add a wall-clock time to an event.

    The UI needs it to say how long the running step has been waiting, which is
    the difference between a deployment that is working and one that is stuck. A
    poll count does not show this: it only says how often we looked.

    Milliseconds since the epoch, so the browser can subtract two of them without
    parsing anything.
    """
    if isinstance(event, dict) and 'at' not in event:
        event = dict(event, at=int(time.time() * 1000))
    return event


def _append_event(deployment_id, event):
    with _DEPLOY_LOCK:
        entry = _DEPLOYMENTS.setdefault(deployment_id, {'events': []})
        entry['events'].append(_stamp(event))
        # Enough to see every step and its waits, without growing without bound
        # if a stack sits in CREATE_IN_PROGRESS for an hour.
        if len(entry['events']) > 500:
            del entry['events'][:len(entry['events']) - 500]
        _persist_locked()


def _plan_for(document, region=None, assign_public_ip=None, preflight_check=True):
    """validate -> resolve -> plan, returning either a plan or a verdict.

    Staged so a config problem is never reported as an AWS error and an AWS
    problem is never reported as a config problem.
    """
    found = problems_for(document)
    if found:
        return None, ({
            'planned': False,
            'stage': 'validate',
            'problems': [{'path': p.path, 'message': p.message} for p in found],
        }, 200)
    try:
        resolution = resolve_link(document, region=region)
    except ResolutionError as exc:
        return None, ({
            'planned': False, 'stage': 'resolve',
            'error': str(exc), 'hint': exc.hint,
        }, 200)
    try:
        plan_doc = add_load_step(build_plan(
            resolution, assign_public_ip=assign_public_ip,
            # So the broker it creates will admit this process. Discovered rather
            # than configured per link: it is a property of this control plane.
            monitor_security_group_id=control_plane_security_group()))
    except PlanError as exc:
        return None, ({
            'planned': False, 'stage': 'plan',
            'error': str(exc), 'hint': exc.hint,
        }, 200)

    # Source-schema preflight: do the configured tables, and the columns the
    # transforms reference, actually exist on the source? Attached to the
    # resolution so `plan` can show it and `deploy` can refuse a definitive miss.
    # Never fatal here: an unreachable source is a warning, not a block, and any
    # unexpected failure degrades to a warning rather than breaking planning.
    if preflight_check:
        try:
            extra = {}
            if (resolution.get('source') or {}).get('engine') == 'postgresql':
                extra['publication'] = _pg_publication(resolution, plan_doc, region)
            resolution['preflight'] = preflight_source(document, region=region, **extra)
        except Exception as exc:  # noqa: BLE001
            resolution['preflight'] = {
                'checked': False, 'blocks': 0, 'warnings': 1,
                'findings': [{'level': 'warn', 'path': 'source',
                              'message': 'preflight could not run: {}'.format(exc)}],
            }
        _check_key_conflicts(resolution, region)
        _check_target_memory(resolution, region)
        for finding in target_findings(resolution.get('target')):
            _add_finding(resolution, finding.level, finding.path, finding.message)
        # Jira story F171: a secret the task-role boundary does not admit stops the tasks.
        for finding in secret_findings(resolution, pipeline_secret_list()):
            _add_finding(resolution, finding.level, finding.path, finding.message)
    return (resolution, plan_doc), None


def _pg_publication(resolution, plan_doc, region):
    """The PostgreSQL publication the link's Debezium reads: the deployed CDC
    stack's, or the link's own for a new link. None when the stack cannot be
    read, which makes the preflight's publication check a warning."""
    try:
        import boto3
        cfn = boto3.client('cloudformation', region_name=region) if region \
            else boto3.client('cloudformation')
        return deployed_pg_publication(cfn, resolution['name'],
                                       (plan_doc or {}).get('prefix') or 'ecdl')
    except Exception:  # noqa: BLE001 -- unknown is a warning, not a failure
        return None


# Warn at this level of target memory use. The loader enforces the real check
# (an estimate of what this load needs against what is free) before it writes
# anything; this only says up front that the target is already nearly full.
TARGET_MEMORY_WARN_PCT = 80.0


def _check_target_memory(resolution, region):
    members = (resolution.get('target') or {}).get('member_clusters') or []
    if not members:
        return
    try:
        import datetime
        import boto3
        cw = boto3.client('cloudwatch', region_name=region) if region \
            else boto3.client('cloudwatch')
        end = datetime.datetime.utcnow()
        worst = None
        for member in members:
            points = cw.get_metric_statistics(
                Namespace='AWS/ElastiCache', MetricName='DatabaseMemoryUsagePercentage',
                Dimensions=[{'Name': 'CacheClusterId', 'Value': member}],
                StartTime=end - datetime.timedelta(minutes=15), EndTime=end,
                Period=300, Statistics=['Maximum']).get('Datapoints') or []
            for point in points:
                value = point.get('Maximum')
                if value is not None and (worst is None or value > worst):
                    worst = value
    except Exception:  # noqa: BLE001 -- a missing metric must not block planning
        return
    if worst is not None and worst >= TARGET_MEMORY_WARN_PCT:
        _add_finding(resolution, 'warn', 'target',
                     'the target cache is already {:.0f}% full. The bulk load '
                     'checks whether this link fits before writing and stops if '
                     'it does not; a larger node type or more shards may be '
                     'needed.'.format(worst))


# A finding's kind for a key clash with another link (Jira story F160). ignore_preflight
# does not override it; see _deploy.
KEY_CONFLICT = 'key_conflict'


def _add_finding(resolution, level, path, message, kind=None):
    pre = resolution.setdefault('preflight', {
        'checked': False, 'blocks': 0, 'warnings': 0, 'findings': []})
    finding = {'level': level, 'path': path, 'message': message}
    if kind:
        finding['kind'] = kind
    pre.setdefault('findings', []).append(finding)
    key = 'blocks' if level == 'block' else 'warnings'
    pre[key] = pre.get(key, 0) + 1


def _check_key_conflicts(resolution, region):
    """Refuse a link that would write keys another deployed link writes.

    A block, not a warning: two links writing <table>:<pk> into one cache
    overwrite each other's rows, and the second bulk load can fill the cache.
    """
    try:
        import boto3
        cfn = boto3.client('cloudformation', region_name=region) if region \
            else boto3.client('cloudformation')
        clashes = resolution_conflicts(cfn, resolution)
    except Exception as exc:  # noqa: BLE001
        _add_finding(resolution, 'warn', 'target',
                     'could not check whether another pipeline already writes these '
                     'keys into this cache: {}'.format(exc))
        return
    host = (resolution.get('target') or {}).get('endpoint')
    for clash in clashes:
        _add_finding(resolution, 'block', 'target',
                     '{} {}'.format(describe_conflict(clash, host), CONFLICT_HINT),
                     kind=KEY_CONFLICT)


@links_bp.route('/plan', methods=['POST'])
def plan_endpoint():
    """What would be deployed, in what order, with which parameters.

    Read-only. Separate from deploy so the plan can be reviewed, and so it can be
    diffed against what is already deployed without any risk of changing it.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({
            'error': 'send the link config as a JSON object in the request body',
        }), 400
    document = body.get('link') if isinstance(body.get('link'), dict) else body

    ok, verdict = _plan_for(document, region=body.get('region'),
                            assign_public_ip=body.get('assign_public_ip'))
    if verdict:
        payload, status = verdict
        return jsonify(payload), status
    resolution, plan_doc = ok
    resolution.pop('link', None)
    return jsonify({
        'planned': True,
        'plan': plan_doc,
        'summary': describe_plan(plan_doc),
        'resolution': resolution,
        'resolution_summary': describe_resolution(resolution),
        # Lifted to the top level so the review UI can annotate fields without
        # reading the nested resolution object.
        'preflight': resolution.get('preflight'),
    })


@links_bp.route('/deploy', methods=['POST'])
def deploy_endpoint():
    """Deploy the link. Returns immediately with an id to poll.

    The work runs on a background thread because a full deploy waits on
    ElastiCache and takes minutes. Returning as soon as the API calls were
    accepted would make "deployed" and "working" different things with nothing to
    tell them apart, so the record only reaches `succeeded` once every stack has
    reached a usable state and every load task has exited zero.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({
            'error': 'send the link config as a JSON object in the request body',
        }), 400
    source = _request_source()
    payload, status = _deploy(body, source=source)
    if not body.get('dry_run'):
        _audit_response('deploy', payload, status, link=_link_name(body), source=source)
    return jsonify(payload), status


def _link_name(body):
    """The link name a deploy request is about, for the audit line."""
    document = body.get('link') if isinstance(body.get('link'), dict) else body
    name = document.get('name') if isinstance(document, dict) else None
    return name if isinstance(name, str) else None


def _review_update(resolution, document, clients, assign_public_ip=None):
    """Compare the update this document would make with the deployed stacks.

    Returns (review, follow_up_plan, outstanding, update_plan); review is None
    when none of the link's stacks exist. Shared by /deploy and /repair, and the
    same check `ecdl apply` runs (ecdl_link.change).
    """
    update_plan = add_load_step(build_plan(
        resolution, assign_public_ip=assign_public_ip, for_update=True,
        monitor_security_group_id=control_plane_security_group()))
    review = review_deployed(clients.cloudformation, update_plan,
                             normalize(document), _template_body)
    if review is None:
        return None, None, [], update_plan
    plan, outstanding = follow_up(review, resolution)
    return review, plan, outstanding, update_plan


def _review_payload(review, plan, outstanding):
    return {
        'review': {'severity': review['diff']['severity'],
                   'changes': review['diff']['changes'],
                   'summary': describe_review(review)},
        'follow_up': plan,
        'outstanding': outstanding,
    }


def _deploy(body, action='deploy', source=None):
    """Deploy or update a link: (payload, HTTP status). The one change path
    (D7): /deploy and /repair with apply both come here, so both get the
    same state check, review, lock, conflict check and record. `action` and
    `source` are for the audit line written when the run finishes."""
    document = body.get('link') if isinstance(body.get('link'), dict) else body
    region = body.get('region')

    ok, verdict = _plan_for(document, region=region,
                            assign_public_ip=body.get('assign_public_ip'))
    if verdict:
        payload, status = verdict
        payload['deployed'] = False
        return payload, status
    resolution, plan_doc = ok

    preflight = resolution.get('preflight') or {}

    if body.get('dry_run'):
        # An explicit flag, so "show me what this would do" and "do it"
        # are the same request with one flag rather than two code paths that can
        # disagree.
        return ({
            'deployed': False, 'dry_run': True,
            'plan': plan_doc, 'summary': describe_plan(plan_doc),
            'preflight': preflight,
        }), 200

    # Another deployed link already writes these keys into this cache (Jira story F160).
    # Refused whatever the caller says: the two links would overwrite each
    # other's rows with no error, and the fix is always available and safe
    # (advanced.key_prefix, or another cache). ignore_preflight exists for
    # facts about the source that may change by the time capture starts; a
    # key clash does not go away on its own, so no override is offered.
    clashes = [f for f in preflight.get('findings') or []
               if f.get('level') == 'block' and f.get('kind') == KEY_CONFLICT]
    if clashes:
        return ({
            'deployed': False, 'stage': 'key_conflict',
            'link': (document or {}).get('name') if isinstance(document, dict) else None,
            'preflight': preflight,
            'message': ' '.join(f.get('message', '') for f in clashes),
            'hint': ('Set advanced.key_prefix on this link or use another target '
                     'cache. "ignore_preflight" does not override this. If the other '
                     'pipeline is gone, delete what is left of its stacks.'),
        }), 409

    # Refuse a deploy whose tables or columns do not exist on the source. This is
    # only reached when the source WAS read (a `block`); an unreachable source
    # yields warnings, not blocks, so it never triggers this check. `ignore_preflight` is
    # the override for the legitimate case where the table will exist by the
    # time capture starts (e.g. created as part of the same change).
    # Log-retention blocks (the load's pause would outlast the source's log)
    # only count once it is known that this deploy pauses for a load; an
    # update of a running link does not.
    retention_blocks = sum(1 for f in preflight.get('findings') or []
                           if f.get('level') == 'block' and f.get('path') == RETENTION_PATH)
    if (preflight.get('blocks') or 0) > retention_blocks and not body.get('ignore_preflight'):
        return ({
            'deployed': False,
            'stage': 'preflight',
            'preflight': preflight,
            # The block messages themselves: a block is no longer only a missing
            # table or column (a key clash with another pipeline is one too).
            'message': ' '.join(f.get('message', '') for f in
                                (preflight.get('findings') or [])
                                if f.get('level') == 'block'),
            'hint': ('Fix what is listed, or resend with "ignore_preflight": true '
                     'to deploy anyway (it does not cover a key clash with another '
                     'pipeline).'),
        }), 409

    try:
        clients = DeployClients(region=region)
        _deploy_settings()
    except DeployError as exc:
        return ({'deployed': False, 'stage': 'clients',
                        'error': str(exc), 'hint': exc.hint}), 200

    # Already deployed? Refuse unless the caller confirms.
    #
    # Without this a second deploy of the same link "succeeds" silently: the
    # deployer is idempotent by design, so CloudFormation answers a re-create of
    # a healthy stack with "No updates are to be performed" and the record still
    # reaches `succeeded`. That is correct behaviour with no signal, which is how
    # the same pipeline gets deployed twice without anyone noticing. Naming it
    # lets the UI ask "this already exists -- update it?" instead.
    #
    # The guard reads EVERY stack state, not just the healthy ones. It used to
    # count only finished stacks, so a link whose stack was mid-update (another
    # deploy, from this process or the CLI) looked "not deployed" and a second
    # deploy started while it was still running.
    try:
        # With ECS: a running load of this link counts as in progress (it holds
        # capture paused while every stack reads COMPLETE).
        current = deployment_state(plan_doc, clients.cloudformation, ecs=clients.ecs)
    except Exception as exc:  # noqa: BLE001
        return ({
            'deployed': False, 'stage': 'state',
            'error': 'could not read the current stack states: {}'.format(exc),
            'hint': 'Refusing to deploy without knowing what already exists. '
                    'Check the control plane can call cloudformation:DescribeStacks '
                    'and ecs:ListTasks.',
        }), 200
    if current['state'] == 'in_progress':
        busy = ', '.join('{} {}'.format(n, s) for n, s in sorted(current['stacks'].items()))
        if current.get('loader_tasks'):
            busy = 'a load is running ({} task(s))'.format(len(current['loader_tasks']))
        return ({
            'deployed': False, 'stage': 'in_progress', 'link': plan_doc['link'],
            'existing_stacks': current['stacks'],
            'loader_tasks': current.get('loader_tasks') or [],
            'message': 'A deployment of {!r} is already running ({}).'.format(
                plan_doc['link'], busy),
            'hint': 'Wait for it to finish, then deploy again.',
        }), 409
    # The last load did not finish: an update would load nothing and report
    # success while the cache is missing rows.
    refusal = initial_load_refusal(plan_doc, current, reload=bool(body.get('reload')))
    if refusal is not None:
        return ({
            'deployed': False, 'stage': 'initial_load', 'link': plan_doc['link'],
            'initial_load': current.get('initial_load'),
            'message': str(refusal), 'hint': refusal.hint,
        }), 409

    # Compare with what is deployed (D7). It used to decide from stack states
    # alone, so a new target or source was applied in place and an edit that
    # left out advanced.* reset key_prefix, alarms and sizing to defaults.
    review = follow = None
    outstanding = []
    update_plan = None
    if current['stacks']:
        try:
            review, follow, outstanding, update_plan = _review_update(
                resolution, document, clients, body.get('assign_public_ip'))
        except PlanError as exc:
            return ({'deployed': False, 'stage': 'plan',
                     'error': str(exc), 'hint': exc.hint}), 200
        except Exception as exc:  # noqa: BLE001
            return ({
                'deployed': False, 'stage': 'review',
                'error': 'could not compare the change with the deployed link: {}'.format(exc),
                'hint': 'Refusing to update a running link without knowing what '
                        'the update changes.',
            }), 200
    if review and review['refuse']:
        return dict({
            'deployed': False, 'stage': 'review', 'link': plan_doc['link'],
            'message': 'This change cannot be made to the running link.',
            'hint': 'Deploy it as a new link, or change the document back.',
        }, **_review_payload(review, follow, outstanding)), 409
    if review and review['needs_ack'] and not body.get('acknowledge'):
        return dict({
            'deployed': False, 'stage': 'acknowledge', 'link': plan_doc['link'],
            'message': ('After this update the cache needs a backfill or a '
                        'cleanup, listed under "outstanding".'),
            'hint': 'Send the same request with "acknowledge": true to update, '
                    'then run the outstanding steps.',
        }, **_review_payload(review, follow, outstanding)), 409
    # Only SERVING stacks make this a redeploy. A failed first attempt is a
    # retry, not a redeploy (see the comment on _DEPLOYED_STATES).
    existing = {n: s for n, s in current['stacks'].items()
                if s in _DEPLOYED_STATES}
    if existing and not body.get('confirm_redeploy'):
        if existing:
            return ({
                'deployed': False,
                'already_deployed': True,
                'link': plan_doc['link'],
                'existing_stacks': existing,
                'message': ('A pipeline for {!r} is already deployed ({}). '
                            'Deploying again updates it in place; it does not '
                            'create a second copy.'.format(
                                plan_doc['link'],
                                ', '.join('{} {}'.format(n, s)
                                          for n, s in sorted(existing.items())))),
                'hint': 'Send the same request with "confirm_redeploy": true to '
                        'update the existing pipeline, or delete its stacks first '
                        'to deploy from the beginning.',
            }), 409

    # A redeploy of a fully deployed CDC link updates the stacks and leaves
    # capture and the cache alone. It used to re-run the FIRST-deploy plan, which
    # paused capture and bulk-loaded every table again for a one-line change.
    # `reload: true` asks for the full load explicitly (e.g. after a failed load).
    if wants_update_plan(plan_doc, current['state'], reload=bool(body.get('reload'))):
        try:
            plan_doc = update_plan or add_load_step(build_plan(
                resolution, assign_public_ip=body.get('assign_public_ip'),
                for_update=True,
                monitor_security_group_id=control_plane_security_group()))
        except PlanError as exc:
            return ({'deployed': False, 'stage': 'plan',
                            'error': str(exc), 'hint': exc.hint}), 200

    if not plan_pauses_capture(plan_doc):
        # Nothing is paused, so how long a load would hold capture is moot.
        preflight = without_path(preflight, RETENTION_PATH) if preflight else preflight
    elif retention_blocks and not body.get('ignore_preflight'):
        return ({
            'deployed': False, 'stage': 'preflight', 'link': plan_doc['link'],
            'preflight': preflight,
            'message': ' '.join(f.get('message', '') for f in
                                (preflight.get('findings') or [])
                                if f.get('level') == 'block'
                                and f.get('path') == RETENTION_PATH),
            'hint': ('Raise the source\'s log retention as shown, or resend with '
                     '"ignore_preflight": true to deploy anyway.'),
        }), 409

    # Start capture again from the source's current position (Jira story F057): only with
    # a reload of a link that loads, and for PostgreSQL once the operator has
    # dropped the slot. Whether the broker can be read is checked by the
    # deployer before it changes anything.
    reset = bool(body.get('reset_position'))
    pg_slot_dropped = bool(body.get('pg_slot_dropped'))
    refusal = reset_position_refusal(plan_doc, reload=bool(body.get('reload')),
                                     reset_position=reset, pg_slot_dropped=pg_slot_dropped)
    if refusal is not None:
        return ({
            'deployed': False, 'stage': 'reset_position', 'link': plan_doc['link'],
            'message': str(refusal), 'hint': refusal.hint,
        }), 409

    deployment_id = 'deploy-{}-{}'.format(plan_doc['link'], uuid.uuid4().hex[:8])
    with _DEPLOY_LOCK:
        running = _RUNNING_LINKS.get(plan_doc['link'])
        if running:
            return ({
                'deployed': False, 'stage': 'in_progress', 'link': plan_doc['link'],
                'deployment_id': running,
                'message': 'A deployment of {!r} is already running ({}).'.format(
                    plan_doc['link'], running),
                'hint': 'Wait for it to finish, then deploy again.',
                'poll': '/api/links/deployments/{}'.format(running),
            }), 409
        _RUNNING_LINKS[plan_doc['link']] = deployment_id
    debezium_service = next(
        ('{}-debezium'.format(s['name']) for s in plan_doc['steps']
         if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
    _record(deployment_id, id=deployment_id, link=plan_doc['link'],
            mode=plan_doc['mode'], status='running', error=None, hint=None,
            steps=[s['id'] for s in plan_doc['steps']], completed=[],
            for_update=bool(plan_doc.get('for_update')),
            reset_position=reset,
            cluster_name=plan_doc.get('cluster_name'),
            debezium_service=debezium_service,
            started_at=int(time.time() * 1000),
            review=(_review_payload(review, follow, outstanding)['review']
                    if review else None),
            outstanding=outstanding, action=action,
            requested_by=source if source is not None else _request_source())
    requested_by = _DEPLOYMENTS[deployment_id].get('requested_by')

    def progress(event):
        _append_event(deployment_id, event)
        if event.get('event') == 'stack_complete':
            with _DEPLOY_LOCK:
                _DEPLOYMENTS[deployment_id]['completed'].append(event['step'])
        change = capture_pause_transition(event)
        if change is not None:
            # On the record itself, outside the capped event list (Jira story F103).
            _record(deployment_id, capture_pause=None if change == 'resumed' else change,
                    capture_paused_at=(None if change == 'resumed' else
                                       _DEPLOYMENTS[deployment_id].get('capture_paused_at')
                                       or int(time.time() * 1000)))

    def worker():
        try:
            # The probe reads the committed offset from the broker, which this
            # control plane can reach through the monitor security group. If it
            # cannot, the deployer falls back to a timed grace period.
            probe = None
            try:
                probe = broker_offset_probe(clients.secrets())
            except Exception:  # noqa: BLE001
                probe = None
            deployer = Deployer(clients, template_body=_template_body,
                                on_progress=progress, offset_probe=probe,
                                reset_position=reset, pg_slot_dropped=pg_slot_dropped,
                                **_deploy_settings())
            try:
                result = deployer.run(plan_doc)
            except DeployError as exc:
                _record(deployment_id, status='failed', error=str(exc),
                        hint=exc.hint, failed_step=exc.step,
                        capture_paused=not getattr(exc, 'capture_resumed', True))
                _audit(action, plan_doc['link'], 'failed', source=requested_by,
                       deployment_id=deployment_id, failed_step=exc.step, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                _record(deployment_id, status='failed',
                        error='unexpected failure: {}'.format(exc))
                _audit(action, plan_doc['link'], 'failed', source=requested_by,
                       deployment_id=deployment_id, error='unexpected failure: {}'.format(exc))
            else:
                _record(deployment_id, status='succeeded',
                        completed=result['completed'], outputs=result['outputs'],
                        capture_verified=result.get('capture_verified'),
                        load_recorded=result.get('load_recorded'),
                        offset_recorded=result.get('offset_recorded'))
                _audit(action, plan_doc['link'], 'succeeded', source=requested_by,
                       deployment_id=deployment_id, completed=result['completed'])
        finally:
            with _DEPLOY_LOCK:
                if _RUNNING_LINKS.get(plan_doc['link']) == deployment_id:
                    del _RUNNING_LINKS[plan_doc['link']]

    thread = threading.Thread(target=worker, name=deployment_id, daemon=True)
    thread.start()

    out = {
        'deployed': True,
        'deployment_id': deployment_id,
        'status': 'running',
        'plan': plan_doc,
        'summary': describe_plan(plan_doc),
        # Carried through even on success so any warnings (e.g. the source could
        # not be reached to verify) are visible rather than silently dropped.
        'preflight': preflight,
        'poll': '/api/links/deployments/{}'.format(deployment_id),
    }
    if review:
        out.update(_review_payload(review, follow, outstanding))
    return out, 202


# ── deleting a pipeline ──────────────────────────────────────────────────
# The same teardown `ecdl destroy --link` runs: the link's own stacks only
# (cdc, loader, broker, in that order), then its ECS cluster if empty. The
# source database and the target cache are referenced, never owned, so they are
# never touched. There was no way to do this from the UI at all.

_LINK_NAME_RE = re.compile(r'[a-z](?:[a-z0-9-]{0,26}[a-z0-9])?\Z')
_IN_PROGRESS_SUFFIX = '_IN_PROGRESS'


def _stack_statuses(clients, plan_doc):
    """{stack name: status or None} for the stacks a destroy would touch."""
    out = {}
    for step in plan_doc['stacks']:
        try:
            stacks = clients.cloudformation.describe_stacks(
                StackName=step['name']).get('Stacks') or []
            out[step['name']] = stacks[0].get('StackStatus') if stacks else None
        except Exception:  # noqa: BLE001 -- "does not exist"
            out[step['name']] = None
    return out


def _destroy_target(name, region):
    """Validate the name and build the plan + current stack statuses."""
    name = (name or '').strip()
    if not _LINK_NAME_RE.match(name):
        return None, (jsonify({'error': '{!r} is not a valid pipeline name'.format(name)}), 400)
    plan_doc = destroy_plan(name)
    try:
        clients = DeployClients(region=region)
        _deploy_settings()
    except DeployError as exc:
        return None, (jsonify({'error': str(exc), 'hint': exc.hint}), 503)
    return (plan_doc, clients, _stack_statuses(clients, plan_doc)), None


@links_bp.route('/destroy-plan', methods=['GET'])
def destroy_plan_endpoint():
    """What deleting a pipeline would remove and keep. Changes nothing."""
    ok, error = _destroy_target(request.args.get('link'), request.args.get('region'))
    if error:
        return error
    plan_doc, clients, statuses = ok
    # What the broker holds that is not in the cache (Jira story F136): deleting it with
    # unapplied changes or dead letters needs "delete_backlog": true.
    data = (Destroyer(clients).broker_data(plan_doc) if any(statuses.values())
            else {'exists': False, 'at_risk': False})
    return jsonify({
        'plan': plan_doc,
        'summary': describe_destroy_plan(plan_doc),
        'stacks': [{'id': s['id'], 'name': s['name'], 'status': statuses[s['name']]}
                   for s in plan_doc['stacks']],
        'exists': any(statuses.values()),
        'broker': data,
        'broker_summary': describe_broker_data(data),
    })


@links_bp.route('/destroy', methods=['POST'])
def destroy_endpoint():
    """Delete a pipeline. Body: {"link": name, "confirm": name}.

    `confirm` must repeat the name: deleting a pipeline stops replication and
    removes its broker, and that should take more than one click.
    """
    body = request.get_json(silent=True) or {}
    name = str(body.get('link') or '').strip()
    source = _request_source()

    def refuse(payload, status):
        _audit('destroy', name or None, 'refused', source=source, status=status,
               stage=payload.get('stage'), reason=payload.get('error'))
        return jsonify(payload), status

    if str(body.get('confirm') or '').strip() != name or not name:
        return refuse({'error': 'type the pipeline name to confirm the delete'}, 400)
    ok, error = _destroy_target(name, body.get('region'))
    if error:
        response, status = error
        return refuse(response.get_json() or {}, status)
    plan_doc, clients, statuses = ok
    if not any(statuses.values()):
        return refuse({'error': 'pipeline {!r} has no stacks to delete'.format(name)}, 404)
    busy = {n: st for n, st in statuses.items() if st and st.endswith(_IN_PROGRESS_SUFFIX)}
    with _DEPLOY_LOCK:
        running = _RUNNING_LINKS.get(name)
    if busy or running:
        return refuse({
            'error': 'pipeline {!r} is being changed right now ({}); wait for that '
                     'to finish, then delete it'.format(
                         name, ', '.join('{} {}'.format(n, st) for n, st in sorted(busy.items()))
                         or running),
        }, 409)
    # Unapplied changes and dead letters go with the broker (Jira story F136). Asked
    # here, before anything starts, so the answer is this response.
    data = Destroyer(clients).broker_data(plan_doc)
    if data.get('at_risk') and not body.get('delete_backlog'):
        return refuse({
            'stage': 'backlog', 'broker': data,
            'error': 'pipeline {!r} was not deleted: {}'.format(
                name, describe_broker_data(data)),
            'hint': 'Let the sink catch up, or replay the dead letters, then delete '
                    'again; or resend with "delete_backlog": true to delete anyway.',
        }, 409)

    destroy_id = 'destroy-{}-{}'.format(name, uuid.uuid4().hex[:8])
    with _DEPLOY_LOCK:
        if _RUNNING_LINKS.get(name):
            return refuse({'error': 'pipeline {!r} is being changed right now'.format(name)},
                          409)
        _RUNNING_LINKS[name] = destroy_id
    _record(destroy_id, id=destroy_id, link=name, kind='destroy', mode='delete',
            status='running', error=None, hint=None,
            steps=[s['id'] for s in plan_doc['stacks']], completed=[],
            started_at=int(time.time() * 1000), requested_by=source,
            broker_data=data)

    def progress(event):
        _append_event(destroy_id, event)
        if event.get('event') in ('stack_deleted', 'stack_absent'):
            with _DEPLOY_LOCK:
                _DEPLOYMENTS[destroy_id]['completed'].append(event.get('id'))

    def worker():
        try:
            # Checked just above (or confirmed), so the run does not ask again.
            result = Destroyer(clients, on_progress=progress,
                               role_arn=_deploy_settings().get('role_arn'),
                               delete_backlog=True).run(plan_doc)
        except DeployError as exc:
            _record(destroy_id, status='failed', error=str(exc),
                    hint=getattr(exc, 'hint', None))
            _audit('destroy', name, 'failed', source=source, deployment_id=destroy_id,
                   error=str(exc))
        except Exception as exc:  # noqa: BLE001
            _record(destroy_id, status='failed',
                    error='unexpected failure: {}'.format(exc))
            _audit('destroy', name, 'failed', source=source, deployment_id=destroy_id,
                   error='unexpected failure: {}'.format(exc))
        else:
            _record(destroy_id, status='succeeded', deleted=result['deleted'],
                    skipped=result['skipped'], kept=result['kept'],
                    follow_up=result['follow_up'],
                    cluster_deleted=result.get('cluster_deleted'))
            _audit('destroy', name, 'succeeded', source=source, deployment_id=destroy_id,
                   deleted=result['deleted'],
                   unapplied=data.get('unapplied'),
                   dead_letters=data.get('dead_letters_total'))
        finally:
            with _DEPLOY_LOCK:
                if _RUNNING_LINKS.get(name) == destroy_id:
                    del _RUNNING_LINKS[name]

    threading.Thread(target=worker, name=destroy_id, daemon=True).start()
    _audit('destroy', name, 'accepted', source=source, status=202,
           deployment_id=destroy_id, delete_backlog=bool(body.get('delete_backlog')) or None)
    return jsonify({'deleting': True, 'destroy_id': destroy_id, 'status': 'running',
                    'poll': '/api/links/deployments/{}'.format(destroy_id)}), 202


@links_bp.route('/repair', methods=['POST'])
def repair_endpoint():
    """What a change to a link does, and (with apply) carry it out.

    Body: {"desired": {...}, "apply": false, "acknowledge": false,
           "current": {...}}

    When the link is deployed, the change is compared with its STACKS, on the
    server -- the same review /deploy and `ecdl apply` run (ecdl_link.change).
    "current" is used only for a link that is not deployed, as a plain
    comparison of two documents. It used to be trusted always: the UI sent the
    document it had reconstructed, which left out advanced.*, so an edit reset
    key_prefix, alarms, sizing and the CDC user to defaults without showing it.

    With apply the update goes through /deploy's path: background run with a
    record to poll (202), one deployment per link at a time, the state check
    (including a running load), the key-conflict check and the review. It used
    to run synchronously with none of those, and the UI's 120 s timeout turned
    most updates into "request failed" while CloudFormation carried on.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send {"desired": {...}}'}), 400
    current = body.get('current')
    desired = body.get('desired')
    if not isinstance(desired, dict) or (current is not None and not isinstance(current, dict)):
        return jsonify({
            'error': '"desired" must be a link config object ("current" too, if sent)',
        }), 400

    for label, document in (('current', current), ('desired', desired)):
        found = problems_for(document) if document is not None else []
        if found:
            return jsonify({
                'ok': False, 'stage': 'validate', 'which': label,
                'problems': [{'path': p.path, 'message': p.message}
                             for p in found],
            })

    region = body.get('region')
    ok, verdict = _plan_for(desired, region=region,
                            assign_public_ip=body.get('assign_public_ip'),
                            preflight_check=bool(body.get('apply')))
    if verdict:
        payload, status = verdict
        payload['ok'] = False
        return jsonify(payload), status
    resolution, plan_doc = ok
    try:
        clients = DeployClients(region=region)
        state = deployment_state(plan_doc, clients.cloudformation)
    except Exception as exc:  # noqa: BLE001
        return jsonify({'ok': False, 'stage': 'state',
                        'error': 'could not read the deployed stacks: {}'.format(exc)})

    if state['stacks']:
        try:
            review, follow, outstanding, _plan = _review_update(
                resolution, desired, clients, body.get('assign_public_ip'))
        except Exception as exc:  # noqa: BLE001
            return jsonify({'ok': False, 'stage': 'review',
                            'error': 'could not compare with the deployed link: {}'.format(exc)})
        diff_doc = review['diff']
        compared = 'deployed'
    else:
        if current is None:
            return jsonify({'ok': False, 'stage': 'state',
                            'error': '{!r} is not deployed, so there is nothing to compare '
                                     'with; send "current" to compare two documents'
                                     .format(plan_doc['link'])})
        try:
            diff_doc = diff(current, desired)
        except RepairError as exc:
            return jsonify({'ok': False, 'stage': 'diff', 'error': str(exc),
                            'hint': exc.hint})
        review = {'diff': diff_doc, 'refuse': diff_doc['severity'] == 'recreate',
                  'needs_ack': diff_doc['severity'] in ('backfill', 'cleanup')}
        follow, outstanding = follow_up(review, resolution)
        compared = 'documents'

    payload = {
        'ok': True,
        'compared_with': compared,
        'changed': diff_doc['changed'],
        'diff': diff_doc,
        'diff_summary': describe_review(review) if compared == 'deployed'
        else describe_diff(diff_doc),
        'plan': follow,
        'plan_summary': describe_repair(follow),
        'outstanding': outstanding,
        'needs_acknowledge': bool(review['needs_ack']),
        'applied': False,
    }
    if not body.get('apply'):
        return jsonify(payload)
    if compared != 'deployed':
        payload.update(ok=False, stage='state',
                       error='only a deployed link can be repaired; deploy it instead')
        return jsonify(payload)
    if not diff_doc['changed']:
        payload['plan_summary'] = 'nothing to do'
        return jsonify(payload)

    source = _request_source()
    result, status = _deploy({
        'link': desired, 'region': region, 'confirm_redeploy': True,
        'acknowledge': bool(body.get('acknowledge')),
        'assign_public_ip': body.get('assign_public_ip'),
        # The same override /deploy takes. Dropped here, an edit of a link
        # whose source had any preflight block (GTIDs off, say) could not be
        # applied even when the operator asked for it.
        'ignore_preflight': bool(body.get('ignore_preflight')),
    }, action='repair', source=source)
    _audit_response('repair', result, status, link=plan_doc['link'], source=source)
    payload.update(result)
    payload['ok'] = bool(result.get('deployed'))
    payload['applied'] = bool(result.get('deployed'))
    return jsonify(payload), status


@links_bp.route('/deployments/<deployment_id>', methods=['GET'])
def deployment_status(deployment_id):
    with _DEPLOY_LOCK:
        entry = _DEPLOYMENTS.get(deployment_id)
        snapshot = dict(entry) if entry else None

    region = request.args.get('region') or None

    if snapshot is not None:
        # A tracked run knows its steps and events but not the wiring, because the
        # link document is not persisted. Attach the same components block the
        # discovered path builds, so "what is this wired to" is answered here too.
        if 'components' not in snapshot and snapshot.get('link'):
            try:
                clients = DeployClients(region=region)
                enriched = discovered_one(clients, snapshot['link'])
            except DeployError:
                enriched = None
            if enriched and enriched.get('components'):
                snapshot['components'] = enriched['components']
            if enriched:
                _attach_initial_load(snapshot, enriched.get('initial_load'))
        return jsonify(snapshot)

    # Discovered deployments (read from CloudFormation) are not in the in-memory
    # map, so a request for its details used to 404 even though the list showed the row. Rebuild
    # the same enriched record on demand so the detail pane works for them too.
    if deployment_id.startswith('stacks-'):
        link = deployment_id[len('stacks-'):]
        try:
            clients = DeployClients(region=region)
            item = discovered_one(clients, link)
        except DeployError:
            item = None
        if item is not None:
            return jsonify(item)

    return jsonify({'error': 'no such deployment: {}'.format(deployment_id)}), 404


# The three stacks a link can own, and the order they read in.
_STACK_KINDS = ('broker', 'cdc', 'loader')

# Stack statuses worth reporting. Deleted stacks are omitted deliberately: a
# link that has been torn down should disappear from the list, not remain as a
# row for a pipeline that no longer exists.
_LIVE_STACK_STATUSES = (
    'CREATE_IN_PROGRESS', 'CREATE_COMPLETE', 'CREATE_FAILED',
    'UPDATE_IN_PROGRESS', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE',
    'UPDATE_ROLLBACK_FAILED', 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
    'ROLLBACK_COMPLETE', 'ROLLBACK_FAILED', 'ROLLBACK_IN_PROGRESS',
    'DELETE_IN_PROGRESS', 'DELETE_FAILED', 'IMPORT_COMPLETE',
)


def _status_from_stacks(stacks):
    """One status for a link, from the states of its stacks.

    Deletion is checked first and named separately. Otherwise a teardown reads as
    'running', which on a page titled Deployments looks like a deploy in progress
    -- the opposite of what is happening.
    """
    states = [s['status'] for s in stacks.values()]
    if any(s == 'DELETE_IN_PROGRESS' for s in states):
        return 'deleting'
    if any(s == 'DELETE_FAILED' for s in states):
        return 'failed'
    if any(s.endswith('_IN_PROGRESS') for s in states):
        return 'running'
    # UPDATE_ROLLBACK_COMPLETE is a failed update: the stack runs its PREVIOUS
    # configuration. It matched neither test and read 'succeeded'.
    if any('FAILED' in s or 'ROLLBACK' in s for s in states):
        return 'failed'
    return 'succeeded'


# The sink's dead-letter prefix. Not a stack parameter -- it is the Go sink's
# DLQ_STREAM_PREFIX default -- so it is named here to derive each stream's DLQ
# name without having to read the sink task definition.
_DLQ_PREFIX = 'dlq:'


def _cdc_wiring(clients, cdc_stack_name):
    """Source, broker, target and stream wiring for a link.

    Read from the CDC stack's PARAMETERS, which are the durable record of what
    the Debezium and Go sink tasks actually run with. This is what lets the page
    show a link's topology without the link document: the control plane does not
    persist that document, but CloudFormation keeps every value the stack was
    deployed with. Returns None on any read failure so one bad describe degrades
    to "no wiring shown" rather than blanking the whole deployments page.
    """
    try:
        described = clients.cloudformation.describe_stacks(StackName=cdc_stack_name)
    except Exception:                                     # noqa: BLE001
        return None
    stacks = described.get('Stacks') or []
    if not stacks:
        return None
    p = {x['ParameterKey']: x.get('ParameterValue')
         for x in stacks[0].get('Parameters') or []}
    o = {x['OutputKey']: x.get('OutputValue')
         for x in stacks[0].get('Outputs') or []}
    streams = [s.strip() for s in (p.get('Streams') or '').split(',') if s.strip()]
    return {
        'source': {
            'engine': p.get('SourceEngine'),
            'host': p.get('SourceDBHost'),
            'port': p.get('SourceDBPort'),
            'database': p.get('SourceDBName'),
            'user': p.get('SourceDBUser'),
            'tables': p.get('Tables'),
        },
        'broker': {
            'host': p.get('BrokerHost'),
            'port': p.get('BrokerPort'),
            'tls': p.get('BrokerSSL'),
            'offset_key': p.get('OffsetKey'),
            'schema_history_key': p.get('SchemaHistoryKey'),
        },
        'target': {
            'host': p.get('TargetHost'),
            'port': p.get('TargetPort'),
            'tls': p.get('TargetSSL'),
            'cluster_mode': p.get('TargetCluster'),
        },
        # Each stream and the DLQ its poison/failed events land in, so the wiring
        # names the failure path as well as the happy one.
        'streams': [{'stream': s, 'dlq': _DLQ_PREFIX + s} for s in streams],
        'topic_prefix': p.get('TopicPrefix'),
        'runtime': {
            'cluster': p.get('ClusterName'),
            'debezium_service': o.get('DebeziumService'),
            'sink_service': o.get('SinkService'),
            'sink_scaling': o.get('SinkScaling'),
            'services': [],
        },
    }


def _service_status(clients, cluster, service_names):
    """Live desired/running counts for a link's ECS services.

    The 'is it actually running' that a stack status cannot answer: a stack sits
    at CREATE_COMPLETE while its Fargate service is 0/1 because the task keeps
    crashing. Failures are ignored -- a describe error must degrade this one
    field, never blank the page.
    """
    names = [n for n in service_names if n]
    if not cluster or not names:
        return []
    try:
        desc = clients.ecs.describe_services(cluster=cluster, services=names)
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for s in desc.get('services') or []:
        out.append({
            'name': s.get('serviceName'),
            'status': s.get('status'),
            'desired': s.get('desiredCount'),
            'running': s.get('runningCount'),
        })
    return out


def _stack_index(clients):
    """Map each ecdl link to its broker/cdc/loader stacks. (index, problem)."""
    found = {}
    try:
        token = None
        while True:
            params = {'StackStatusFilter': list(_LIVE_STACK_STATUSES)}
            if token:
                params['NextToken'] = token
            page = clients.cloudformation.list_stacks(**params)
            for summary in page.get('StackSummaries') or []:
                name = summary.get('StackName') or ''
                if not name.startswith('ecdl-') or '-' not in name:
                    continue
                base, _, kind = name.rpartition('-')
                # Split on the LAST hyphen so a link whose own name contains one
                # ('test-source-dbcluster-p') still resolves.
                if kind not in _STACK_KINDS:
                    continue
                link = base[len('ecdl-'):]
                if not link:
                    continue
                entry = found.setdefault(link, {})
                entry[kind] = {
                    'stack': name,
                    'status': summary.get('StackStatus'),
                    'updated': str(summary.get('LastUpdatedTime')
                                   or summary.get('CreationTime') or ''),
                }
            token = page.get('NextToken')
            if not token:
                break
    except Exception as exc:                              # noqa: BLE001
        return {}, 'could not list stacks: {}'.format(exc)
    return found, None


def _discovered_item(clients, link, stacks, with_live=True):
    """One discovered-deployment record, enriched with wiring and live status.

    `with_live` describes the ECS services too; it is on by default because the
    purpose of this record is to answer "what is deployed and is it running"
    in one place, which is the question that otherwise requires the CLI.
    """
    newest = max((s['updated'] for s in stacks.values()), default='')
    item = {
        # Not a real deployment id -- there was no tracked run -- so it is
        # prefixed to make that obvious rather than looking like one.
        'id': 'stacks-{}'.format(link),
        'link': link,
        'status': _status_from_stacks(stacks),
        'source': 'cloudformation',
        'stacks': stacks,
        'steps': [k for k in _STACK_KINDS if k in stacks],
        'completed': [k for k in _STACK_KINDS
                      if stacks.get(k, {}).get('status', '').endswith('_COMPLETE')],
        'updated': newest,
        'note': 'read from CloudFormation, not from a tracked run in this '
                'process, so it has no step timings or event log',
    }
    _attach_initial_load(item, _initial_load_state(clients, stacks))
    cdc = stacks.get('cdc')
    if cdc and cdc.get('stack'):
        wiring = _cdc_wiring(clients, cdc['stack'])
        if wiring:
            if with_live:
                rt = wiring['runtime']
                rt['services'] = _service_status(
                    clients, rt.get('cluster'),
                    [rt.get('debezium_service'), rt.get('sink_service')])
                _attach_capture_paused(item, rt)
            item['components'] = wiring
    return item


CAPTURE_PAUSED_NOTE = (
    'capture is paused: the Debezium service desired count is 0. That is '
    'expected only while a bulk load runs; otherwise changes wait in the '
    "source's log and are lost once its retention runs out. Set the desired "
    'count to 1, or deploy again (an update resumes capture).')


def _attach_capture_paused(item, runtime):
    """Show a link whose stacks are healthy but whose Debezium is held at 0
    as 'paused', not 'succeeded' (Jira story F135)."""
    name = runtime.get('debezium_service')
    dbz = next((s for s in runtime.get('services') or [] if s.get('name') == name), None)
    if dbz is None or dbz.get('status') == 'INACTIVE' or dbz.get('desired') is None:
        return
    if int(dbz.get('desired') or 0) == 0:
        item['capture_paused'] = True
        item['capture_paused_note'] = CAPTURE_PAUSED_NOTE
        if item.get('status') == 'succeeded':
            item['status'] = 'paused'


INITIAL_LOAD_INCOMPLETE_NOTE = (
    'initial load incomplete: the last bulk load did not finish, so the cache may '
    'be missing rows. Deploy again with a reload to run it.')


def _initial_load_state(clients, stacks):
    """The loader stack's load marker (deploy.LOAD_MARKER_TAG), or None.

    None too when there is no loader stack or it cannot be described: a read
    error degrades this one field.
    """
    loader = (stacks or {}).get('loader') or {}
    if not loader.get('stack'):
        return None
    try:
        described = clients.cloudformation.describe_stacks(StackName=loader['stack'])
    except Exception:                                     # noqa: BLE001
        return None
    items = described.get('Stacks') or []
    return load_marker(items[0].get('Tags')) if items else None


def _attach_initial_load(item, state):
    item['initial_load'] = state
    if state == LOAD_INCOMPLETE:
        item['initial_load_incomplete'] = True
        item['initial_load_note'] = INITIAL_LOAD_INCOMPLETE_NOTE
    else:
        item.pop('initial_load_incomplete', None)
        item.pop('initial_load_note', None)


def discovered_one(clients, link):
    """Rebuild one discovered deployment on demand, for the detail endpoint.

    The list carries every discovered link, but the detail request fetches by id and
    these records live only in CloudFormation, so without this a discovered row
    returned 404 when it was clicked.
    """
    found, problem = _stack_index(clients)
    if problem or link not in found:
        return None
    return _discovered_item(clients, link, found[link])


def discovered_deployments(region=None):
    """Links that exist in CloudFormation, whichever process deployed them.

    The in-memory record below is the richer view -- it has the step list and the
    event stream -- but it is also the only view, and it lives in this process.
    Restart the control plane and the page went blank while three stacks and two
    Fargate services continued running, which reads as "nothing is deployed"
    when the truth is the opposite. It is also blank for anything deployed by
    `ecdl apply` from a laptop, which produces identical stacks.

    CloudFormation is the durable record, so it is read as well and merged.
    """
    try:
        clients = DeployClients(region=region)
    except DeployError as exc:
        return [], str(exc)

    found, problem = _stack_index(clients)
    if problem:
        return [], problem

    items = [_discovered_item(clients, link, stacks)
             for link, stacks in found.items()]
    items.sort(key=lambda item: item['updated'], reverse=True)
    return items, None


@links_bp.route('/deployments', methods=['GET'])
def deployment_list():
    with _DEPLOY_LOCK:
        tracked = [{k: v for k, v in entry.items() if k != 'events'}
                   for entry in _DEPLOYMENTS.values()]

    # A tracked run takes precedence: it describes the same stacks in more detail.
    tracked_links = {entry.get('link') for entry in tracked}
    discovered, problem = discovered_deployments(
        region=request.args.get('region') or None)
    extra = [item for item in discovered if item['link'] not in tracked_links]

    # The records are a log of runs, so a pipeline deleted since (from this page
    # or the CLI) still had its old "failed"/"succeeded" row, read as a pipeline
    # that exists. Say which ones no longer have any stacks -- only when the
    # stack list could be read, so a permissions problem never marks a live
    # pipeline as gone.
    if not problem:
        live = {item['link'] for item in discovered}
        for entry in tracked:
            if entry.get('status') != 'running' and entry.get('link') not in live:
                entry['gone'] = True
        # The load marker is on the stacks, not in the run record: carry it over.
        by_link = {item['link']: item for item in discovered}
        for entry in tracked:
            if entry.get('link') in by_link:
                _attach_initial_load(entry, by_link[entry['link']].get('initial_load'))
                # And a capture held at 0 since (Jira story F135): the row shows the
                # link, so a finished deploy of a now-paused link reads
                # 'paused'. run_status keeps what the run itself did.
                live = by_link[entry['link']]
                if live.get('capture_paused') and entry.get('status') == 'succeeded':
                    entry.update(status='paused', run_status='succeeded',
                                 capture_paused_note=live.get('capture_paused_note'))

    payload = {'deployments': tracked + extra,
               'tracked': len(tracked), 'discovered': len(extra)}
    if problem:
        payload['problem'] = problem
    return jsonify(payload)


# ── reconstruct a link from what is deployed ─────────────────────────────────
#
# The control plane does not persist the link document, but the CDC stack keeps
# every value it was deployed with. This rebuilds the link file from those
# parameters so a running pipeline can be edited without anyone writing or
# pasting YAML manually. The one thing a stack records by ENDPOINT rather than by ARN is
# the source and the target, so those two ARNs are recovered by matching the
# stored endpoint against what the account holds (discover()).

def _stack_param_map(clients, stack_name):
    """A stack's Parameters as a dict, or {} on any read failure."""
    try:
        described = clients.cloudformation.describe_stacks(StackName=stack_name)
    except Exception:                                     # noqa: BLE001
        return {}
    stacks = described.get('Stacks') or []
    if not stacks:
        return {}
    return {x['ParameterKey']: x.get('ParameterValue')
            for x in stacks[0].get('Parameters') or []}


def _host_only(endpoint):
    """'clustercfg.x.cache.amazonaws.com:6379' -> the host, lowercased."""
    if not endpoint:
        return ''
    return str(endpoint).split(':', 1)[0].strip().lower()


def _match_arn_by_endpoint(items, host):
    """The ARN of the discovered source/target whose endpoint host matches."""
    host = _host_only(host)
    if not host:
        return None
    for it in items:
        if _host_only(it.get('endpoint')) == host:
            return it.get('arn')
    return None


def reconstruct_link(clients, link, region=None):
    """Rebuild a link document from a deployed pipeline's CloudFormation stacks.

    Returns (document, warnings). `document` is None when there is nothing to read
    (no stacks, or no CDC stack). Anything that could not be recovered becomes a
    warning rather than a failure, so the editor still opens with what was found.
    """
    index, _problem = _stack_index(clients)
    stacks = index.get(link)
    if not stacks:
        return None, ['no deployed stacks were found for "{}"'.format(link)]
    cdc = stacks.get('cdc')
    if not cdc or not cdc.get('stack'):
        return None, [
            '"{}" has no CDC stack, so there is no capture configuration to read '
            '(an initial-load-only link keeps none)'.format(link)]
    p = _stack_param_map(clients, cdc['stack'])
    if not p:
        return None, ['could not read the parameters of {}'.format(cdc['stack'])]

    warnings = []

    # Mode from which stacks exist.
    if 'loader' in stacks and 'cdc' in stacks:
        mode = 'initial_load_and_cdc'
    elif 'cdc' in stacks:
        mode = 'cdc'
    else:
        mode = 'initial_load'

    # Recover the source and target ARNs by matching their deployed endpoints
    # against what the account holds. This is the only part not stored verbatim.
    try:
        inv = discover(region=region)
    except ResolutionError as exc:
        inv = {'sources': [], 'targets': []}
        warnings.append('could not list account resources to recover ARNs ({}); '
                        'fill in source.arn and target.arn manually'
                        .format(exc.message))
    source_arn = _match_arn_by_endpoint(inv.get('sources') or [],
                                        p.get('SourceDBHost'))
    target_arn = _match_arn_by_endpoint(inv.get('targets') or [],
                                        p.get('TargetHost'))
    if not source_arn and (inv.get('sources') is not None):
        warnings.append('could not match the source endpoint {} to a database in '
                        'this account; fill in source.arn manually'
                        .format(p.get('SourceDBHost')))
    if not target_arn and (inv.get('targets') is not None):
        warnings.append('could not match the target endpoint {} to a cache in this '
                        'account; fill in target.arn manually'
                        .format(p.get('TargetHost')))

    tables = [t.strip() for t in (p.get('Tables') or '').split(',') if t.strip()]

    source = {}
    if source_arn:
        source['arn'] = source_arn
    if p.get('SourceDBName'):
        source['database'] = p['SourceDBName']
    if p.get('DBSecretArn'):
        source['secret_arn'] = p['DBSecretArn']

    target = {}
    if target_arn:
        target['arn'] = target_arn

    doc = {
        'schema_version': 1,
        'name': link,
        'mode': mode,
        'source': source,
        'target': target,
        'tables': tables,
    }

    tj = p.get('TransformsJson')
    if tj:
        try:
            parsed = json.loads(tj)
            if isinstance(parsed, dict) and parsed.get('tables'):
                doc['transforms'] = {'tables': parsed['tables']}
        except (ValueError, TypeError):
            warnings.append('the deployed transforms could not be parsed and were '
                            'left out')

    # Every setting, not only the fields above. advanced.* was left out, so an
    # edit through the UI reset key_prefix, alarms, images, CPU architecture,
    # sizing and the CDC user to their defaults (lifecycle C1).
    loader_params = (_stack_param_map(clients, stacks['loader']['stack'])
                     if (stacks.get('loader') or {}).get('stack') else {})
    broker_params = (_stack_param_map(clients, stacks['broker']['stack'])
                     if (stacks.get('broker') or {}).get('stack') else {})
    _reconstruct_settings(doc, p, loader_params, broker_params)
    doc = normalize(doc)
    if source.get('arn') and target.get('arn'):
        warnings.extend(_verify_reconstruct(clients, doc, region))
    else:
        warnings.append('the file could not be checked against the deployed stacks '
                        '(source or target ARN not recovered); review advanced.* '
                        'before applying')
    return doc, warnings


def _planner_defaults(cdc_template_defaults):
    """What the planner sends for a file with no advanced settings."""
    from ecdl_link.provision import DEFAULT_DEBEZIUM, DEFAULT_LOADER, DEFAULT_SINK
    out = dict(cdc_template_defaults or {})
    out.update({
        'DebeziumCpu': str(DEFAULT_DEBEZIUM['cpu']),
        'DebeziumMemory': str(DEFAULT_DEBEZIUM['memory']),
        'SinkWorkers': str(DEFAULT_SINK['workers']),
        'SinkBatchSize': str(DEFAULT_SINK['batch_size']),
        'SinkDesiredCount': str(DEFAULT_SINK['desired_count']),
        'SinkMinCount': str(DEFAULT_SINK['min_count']),
        'SinkMaxCount': str(DEFAULT_SINK['max_count']),
        'SinkTargetCpuPercent': str(DEFAULT_SINK['target_cpu_percent']),
        'LoaderWorkers': str(DEFAULT_LOADER['workers']),
        'TaskCpu': str(DEFAULT_LOADER['cpu']),
        'TaskMemory': str(DEFAULT_LOADER['memory']),
        'KeyPrefix': '',
    })
    return out


def _template_defaults(filename):
    """{param: default} of one of infra/'s templates, or {} if unreadable."""
    from ecdl_link.change import template_body_defaults
    try:
        return template_body_defaults(_template_body(filename))
    except Exception:  # noqa: BLE001
        return {}


def _reconstruct_settings(doc, cdc_params, loader_params, broker_params=None):
    from ecdl_link.change import apply_settings, primary_keys_setting, template_body_defaults
    try:
        tdefaults = template_body_defaults(_template_body('ecdl-cdc-ecs.yaml'))
    except Exception:  # noqa: BLE001
        tdefaults = {}
    defaults = _planner_defaults(tdefaults)
    apply_settings(doc, cdc_params, defaults)
    if loader_params:
        apply_settings(doc, loader_params, defaults,
                       only={'LoaderWorkers', 'TaskCpu', 'TaskMemory'})
        # Only when it differs from the loader template's own pinned image.
        apply_settings(doc, loader_params, _template_defaults('ecdl-loader-ecs.yaml'),
                       only={'LoaderImage'})
        keys = primary_keys_setting(loader_params)
        if keys:
            doc.setdefault('advanced', {}).setdefault('loader', {})['primary_keys'] = keys
    if broker_params:
        # The snapshot the broker was created from (Jira story F022), so the file shows it.
        apply_settings(doc, broker_params, {}, only={'RestoreSnapshotName'})
        apply_settings(doc, broker_params, _template_defaults('ecdl-broker-quickstart.yaml'),
                       only={'NodeType'})


def _verify_reconstruct(clients, doc, region):
    """Plan the rebuilt file as an update and compare it with the stacks. A
    derived value (server id, signal collection, snapshot mode, CDC user) that
    still differs is written in from the stack; anything left is a warning."""
    from ecdl_link.change import INVERSE_IF_DRIFTED, apply_settings, describe_review
    for attempt in range(2):
        try:
            resolution = resolve_link(doc, region=region)
            review, _f, _o, _u = _review_update(resolution, doc, clients)
        except Exception as exc:  # noqa: BLE001
            return ['the rebuilt file could not be checked against the deployed '
                    'stacks ({}); review advanced.* before applying'.format(exc)]
        if review is None or not review['diff']['changed']:
            return []
        drifted = {d['parameter']: d for d in review['drift']}
        fixable = set(drifted) & INVERSE_IF_DRIFTED
        if attempt == 0 and fixable:
            deployed = {k: d['from'] for k, d in drifted.items()}
            planned = {k: d['to'] for k, d in drifted.items()}
            apply_settings(doc, deployed, planned, only=fixable)
            continue
        return ['applying this file unchanged would still change the deployed link: '
                + describe_review(review).replace('\n', ' ')]
    return []


@links_bp.route('/reconstruct', methods=['GET'])
@require_token
def reconstruct_endpoint():
    """Rebuild the link file of a pipeline that is already running.

    So editing a live pipeline needs no manually written or pasted YAML: pick it, and
    this returns its configuration as the same link file the editor and the CLI
    read.
    """
    link = request.args.get('link')
    region = request.args.get('region') or None
    if not link:
        return jsonify({'ok': False, 'error': 'a link name is required'}), 400
    try:
        clients = DeployClients(region=region)
    except DeployError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 200

    doc, warnings = reconstruct_link(clients, link, region=region)
    if doc is None:
        return jsonify({'ok': False,
                        'error': (warnings or ['not found'])[0],
                        'warnings': warnings}), 200

    payload = {'ok': True, 'link': link, 'document': doc, 'warnings': warnings}
    try:
        payload['text'] = to_yaml(doc)
    except LinkValidationError as exc:
        return jsonify({'ok': False,
                        'error': 'could not render the recovered config',
                        'reason': exc.format(), 'warnings': warnings}), 200
    found = problems_for(doc)
    payload['valid'] = not found
    payload['problems'] = [{'path': x.path, 'message': x.message} for x in found]
    return jsonify(payload)


# ── discovery ──────────────────────────────────────────────────────────────
#
# The link schema takes ARNs, which is right for a file that gets reviewed and
# committed but wrong as the first thing a person is asked for. These endpoints
# list what the account actually holds so the form can ask for a source by name
# and fill the ARN in itself.
#
# Protected with `require_token` even though they are GETs. Reads are otherwise
# open, on the reasoning that the read surface is dashboards holding no
# credentials -- but enumerating every database, cache and secret NAME in an
# account is a different kind of read, so it is treated as one.

@links_bp.route('/discover', methods=['GET'])
@require_token
def discover_endpoint():
    """Sources, targets and credentials available to this control plane.

    One call, because a form needs all three to render and three round trips to
    populate one page is three ways for it to load only partly. A failure listing one
    kind is reported against that kind and the rest still come back, so a missing
    secrets permission does not stop anyone choosing a source.

    `for_source` ranks the credentials against a chosen source. That ranking lives
    in the library rather than the browser because a second
    implementation can give a different result.
    """
    contains = request.args.get('contains') or None
    region = request.args.get('region') or None
    for_source = request.args.get('for_source') or None
    try:
        limit = min(int(request.args.get('limit') or DISCOVER_LIMIT),
                    DISCOVER_LIMIT)
    except (TypeError, ValueError):
        limit = DISCOVER_LIMIT

    try:
        found = discover(contains=contains, limit=limit, region=region)
    except ResolutionError as exc:
        # A missing or expired credential is an ordinary operational state, not a
        # defect, so it reads as "refresh your credentials" rather than a 500.
        return jsonify({
            'discovered': False,
            'error': exc.message,
            'hint': exc.hint,
        }), 200

    if for_source:
        match = next((s for s in found['sources'] if s.get('arn') == for_source),
                     None)
        # Rank against the chosen source when we found it; against nothing
        # otherwise, which still returns every secret, just unranked. Refusing
        # would be worse: the ARN may be one the customer typed manually.
        found['secrets'] = secrets_for_source(found['secrets'], match or {})
        found['ranked_for'] = for_source
        if match is None:
            found['problems'] = list(found['problems']) + [{
                'what': 'credential ranking',
                'error': 'no discovered source matches {}'.format(for_source),
                'hint': 'Credentials are listed unranked. This is expected if the '
                        'source ARN was entered manually.',
            }]

    found['discovered'] = True
    return jsonify(found)


@links_bp.route('/discover/tables', methods=['POST'])
@require_token
def discover_tables_endpoint():
    """The tables in a chosen source, so they can be picked rather than typed.

    A POST, not a GET: it needs the source ARN and the credential ARN together,
    and it opens a database connection, which is not a cacheable read.

    This is the one place the control plane reads a secret VALUE, and only ever
    the customer's own database password, only to run a catalogue query, and it
    is never stored or returned. Everything else in this codebase uses only
    describe calls; listing tables genuinely cannot.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send {"link": {...}} or '
                                 '{"source_arn": "...", "secret_arn": "..."}'}), 400

    link = body.get('link') if isinstance(body.get('link'), dict) else None
    if link:
        source = link.get('source') or {}
        source_arn = source.get('arn')
        secret_arn = source.get('secret_arn')
        database = source.get('database')
        schema = source.get('schema')
    else:
        source_arn = body.get('source_arn')
        secret_arn = body.get('secret_arn')
        database = body.get('database')
        schema = body.get('schema')

    if not source_arn or not secret_arn:
        return jsonify({
            'listed': False,
            'stage': 'input',
            'error': 'both a source ARN and a credential ARN are needed to list '
                     'tables',
        }), 200

    try:
        result = list_tables(source_arn=source_arn, secret_arn=secret_arn,
                            database=database, schema=schema,
                            region=body.get('region'))
    except ResolutionError as exc:
        return jsonify({'listed': False, 'stage': 'resolve',
                        'error': exc.message, 'hint': exc.hint}), 200
    return jsonify(result)


@links_bp.route('/format', methods=['POST'])
def format_endpoint():
    """Render a document as the text of a link file.

    The counterpart to /parse, and the reason the guided form and the editor can
    be two views of one thing rather than two sources of truth. The form assembles
    a document, this turns it into the file, and the file is what gets validated,
    reviewed, deployed and committed.

    Serialising in the browser was the alternative and would have meant the text
    someone reviews was written by a different implementation from the one that
    reads it -- the file-versus-form split the schema exists to remove. Also
    returns the verdict, so a form never has to render text it cannot validate.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send {"link": {...}}'}), 400
    document = body.get('link') if isinstance(body.get('link'), dict) else body

    found = problems_for(document)
    payload = {
        'valid': not found,
        'problems': [{'path': p.path, 'message': p.message} for p in found],
    }
    try:
        # Rendered whether or not it validates: a half-finished config still has
        # to be visible in the editor, or the form would appear to lose work
        # every time it is momentarily incomplete.
        payload['text'] = to_yaml(document)
    except LinkValidationError as exc:
        return jsonify({'error': 'could not render the config',
                        'reason': exc.format()}), 500
    if not found:
        payload['normalized'] = normalize(document)
    return jsonify(payload)
