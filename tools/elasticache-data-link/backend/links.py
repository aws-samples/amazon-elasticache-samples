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
from ecdl_link.provision import PlanError, describe_plan, plan as build_plan  # noqa: E402
from ecdl_link.repair import (  # noqa: E402
    RepairError,
    apply_repair,
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
    deployment_state,
    wants_update_plan,
)
from ecdl_link.preflight import preflight as preflight_source  # noqa: E402
from ecdl_link.teardown import (  # noqa: E402
    Destroyer, describe_destroy_plan, destroy_plan,
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
        document = yaml.safe_load(text)
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


def _persist_locked():
    """Write every record to disk. Caller holds _DEPLOY_LOCK. Never raises."""
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
    except Exception:  # noqa: BLE001
        pass   # a full disk must not fail a deploy


def capture_left_paused(record):
    """True if a record's events show capture paused and never resumed."""
    paused = False
    for event in record.get('events') or []:
        if event.get('event') == 'action_complete' and event.get('step') == 'pause_capture':
            paused = True
        elif event.get('event') in ('capture_resumed_after_failure',) or (
                event.get('event') == 'action_complete'
                and event.get('step') == 'resume_capture'):
            paused = False
    return paused


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
                if capture_left_paused(record):
                    record['capture_paused'] = True
                    record['hint'] = (
                        'CAPTURE WAS LEFT PAUSED. Confirm the load task has '
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
            resolution['preflight'] = preflight_source(document, region=region)
        except Exception as exc:  # noqa: BLE001
            resolution['preflight'] = {
                'checked': False, 'blocks': 0, 'warnings': 1,
                'findings': [{'level': 'warn', 'path': 'source',
                              'message': 'preflight could not run: {}'.format(exc)}],
            }
        _check_key_conflicts(resolution, region)
        _check_target_memory(resolution, region)
    return (resolution, plan_doc), None


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


def _add_finding(resolution, level, path, message):
    pre = resolution.setdefault('preflight', {
        'checked': False, 'blocks': 0, 'warnings': 0, 'findings': []})
    pre.setdefault('findings', []).append(
        {'level': level, 'path': path, 'message': message})
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
                     '{} {}'.format(describe_conflict(clash, host), CONFLICT_HINT))


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
    document = body.get('link') if isinstance(body.get('link'), dict) else body
    region = body.get('region')

    ok, verdict = _plan_for(document, region=region,
                            assign_public_ip=body.get('assign_public_ip'))
    if verdict:
        payload, status = verdict
        payload['deployed'] = False
        return jsonify(payload), status
    resolution, plan_doc = ok

    preflight = resolution.get('preflight') or {}

    if body.get('dry_run'):
        # An explicit flag, so "show me what this would do" and "do it"
        # are the same request with one flag rather than two code paths that can
        # disagree.
        return jsonify({
            'deployed': False, 'dry_run': True,
            'plan': plan_doc, 'summary': describe_plan(plan_doc),
            'preflight': preflight,
        })

    # Refuse a deploy whose tables or columns do not exist on the source. This is
    # only reached when the source WAS read (a `block`); an unreachable source
    # yields warnings, not blocks, so it never triggers this check. `ignore_preflight` is
    # the override for the legitimate case where the table will exist by the
    # time capture starts (e.g. created as part of the same change).
    if preflight.get('blocks') and not body.get('ignore_preflight'):
        return jsonify({
            'deployed': False,
            'stage': 'preflight',
            'preflight': preflight,
            # The block messages themselves: a block is no longer only a missing
            # table or column (a key clash with another pipeline is one too).
            'message': ' '.join(f.get('message', '') for f in
                                (preflight.get('findings') or [])
                                if f.get('level') == 'block'),
            'hint': ('Fix what is listed, or resend with "ignore_preflight": true '
                     'to deploy anyway.'),
        }), 409

    try:
        clients = DeployClients(region=region)
    except DeployError as exc:
        return jsonify({'deployed': False, 'stage': 'clients',
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
        current = deployment_state(plan_doc, clients.cloudformation)
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            'deployed': False, 'stage': 'state',
            'error': 'could not read the current stack states: {}'.format(exc),
            'hint': 'Refusing to deploy without knowing what already exists. '
                    'Check the control plane can call cloudformation:DescribeStacks.',
        }), 200
    if current['state'] == 'in_progress':
        return jsonify({
            'deployed': False, 'stage': 'in_progress', 'link': plan_doc['link'],
            'existing_stacks': current['stacks'],
            'message': 'A deployment of {!r} is already running ({}).'.format(
                plan_doc['link'], ', '.join('{} {}'.format(n, s) for n, s in
                                            sorted(current['stacks'].items()))),
            'hint': 'Wait for it to finish, then deploy again.',
        }), 409
    # Only SERVING stacks make this a redeploy. A failed first attempt is a
    # retry, not a redeploy (see the comment on _DEPLOYED_STATES).
    existing = {n: s for n, s in current['stacks'].items()
                if s in _DEPLOYED_STATES}
    if existing and not body.get('confirm_redeploy'):
        if existing:
            return jsonify({
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
            plan_doc = add_load_step(build_plan(
                resolution, assign_public_ip=body.get('assign_public_ip'),
                for_update=True,
                monitor_security_group_id=control_plane_security_group()))
        except PlanError as exc:
            return jsonify({'deployed': False, 'stage': 'plan',
                            'error': str(exc), 'hint': exc.hint}), 200

    deployment_id = 'deploy-{}-{}'.format(plan_doc['link'], uuid.uuid4().hex[:8])
    with _DEPLOY_LOCK:
        running = _RUNNING_LINKS.get(plan_doc['link'])
        if running:
            return jsonify({
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
            cluster_name=plan_doc.get('cluster_name'),
            debezium_service=debezium_service,
            started_at=int(time.time() * 1000))

    def progress(event):
        _append_event(deployment_id, event)
        if event.get('event') == 'stack_complete':
            with _DEPLOY_LOCK:
                _DEPLOYMENTS[deployment_id]['completed'].append(event['step'])

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
                                on_progress=progress, offset_probe=probe)
            try:
                result = deployer.run(plan_doc)
            except DeployError as exc:
                _record(deployment_id, status='failed', error=str(exc),
                        hint=exc.hint, failed_step=exc.step,
                        capture_paused=not getattr(exc, 'capture_resumed', True))
            except Exception as exc:  # noqa: BLE001
                _record(deployment_id, status='failed',
                        error='unexpected failure: {}'.format(exc))
            else:
                _record(deployment_id, status='succeeded',
                        completed=result['completed'], outputs=result['outputs'])
        finally:
            with _DEPLOY_LOCK:
                if _RUNNING_LINKS.get(plan_doc['link']) == deployment_id:
                    del _RUNNING_LINKS[plan_doc['link']]

    thread = threading.Thread(target=worker, name=deployment_id, daemon=True)
    thread.start()

    return jsonify({
        'deployed': True,
        'deployment_id': deployment_id,
        'status': 'running',
        'plan': plan_doc,
        'summary': describe_plan(plan_doc),
        # Carried through even on success so any warnings (e.g. the source could
        # not be reached to verify) are visible rather than silently dropped.
        'preflight': preflight,
        'poll': '/api/links/deployments/{}'.format(deployment_id),
    }), 202


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
    except DeployError as exc:
        return None, (jsonify({'error': str(exc), 'hint': exc.hint}), 503)
    return (plan_doc, clients, _stack_statuses(clients, plan_doc)), None


@links_bp.route('/destroy-plan', methods=['GET'])
def destroy_plan_endpoint():
    """What deleting a pipeline would remove and keep. Changes nothing."""
    ok, error = _destroy_target(request.args.get('link'), request.args.get('region'))
    if error:
        return error
    plan_doc, _clients, statuses = ok
    return jsonify({
        'plan': plan_doc,
        'summary': describe_destroy_plan(plan_doc),
        'stacks': [{'id': s['id'], 'name': s['name'], 'status': statuses[s['name']]}
                   for s in plan_doc['stacks']],
        'exists': any(statuses.values()),
    })


@links_bp.route('/destroy', methods=['POST'])
def destroy_endpoint():
    """Delete a pipeline. Body: {"link": name, "confirm": name}.

    `confirm` must repeat the name: deleting a pipeline stops replication and
    removes its broker, and that should take more than one click.
    """
    body = request.get_json(silent=True) or {}
    name = str(body.get('link') or '').strip()
    if str(body.get('confirm') or '').strip() != name or not name:
        return jsonify({'error': 'type the pipeline name to confirm the delete'}), 400
    ok, error = _destroy_target(name, body.get('region'))
    if error:
        return error
    plan_doc, clients, statuses = ok
    if not any(statuses.values()):
        return jsonify({'error': 'pipeline {!r} has no stacks to delete'.format(name)}), 404
    busy = {n: st for n, st in statuses.items() if st and st.endswith(_IN_PROGRESS_SUFFIX)}
    with _DEPLOY_LOCK:
        running = _RUNNING_LINKS.get(name)
    if busy or running:
        return jsonify({
            'error': 'pipeline {!r} is being changed right now ({}); wait for that '
                     'to finish, then delete it'.format(
                         name, ', '.join('{} {}'.format(n, st) for n, st in sorted(busy.items()))
                         or running),
        }), 409

    destroy_id = 'destroy-{}-{}'.format(name, uuid.uuid4().hex[:8])
    with _DEPLOY_LOCK:
        if _RUNNING_LINKS.get(name):
            return jsonify({'error': 'pipeline {!r} is being changed right now'.format(name)}), 409
        _RUNNING_LINKS[name] = destroy_id
    _record(destroy_id, id=destroy_id, link=name, kind='destroy', mode='delete',
            status='running', error=None, hint=None,
            steps=[s['id'] for s in plan_doc['stacks']], completed=[],
            started_at=int(time.time() * 1000))

    def progress(event):
        _append_event(destroy_id, event)
        if event.get('event') in ('stack_deleted', 'stack_absent'):
            with _DEPLOY_LOCK:
                _DEPLOYMENTS[destroy_id]['completed'].append(event.get('id'))

    def worker():
        try:
            result = Destroyer(clients, on_progress=progress).run(plan_doc)
        except DeployError as exc:
            _record(destroy_id, status='failed', error=str(exc),
                    hint=getattr(exc, 'hint', None))
        except Exception as exc:  # noqa: BLE001
            _record(destroy_id, status='failed',
                    error='unexpected failure: {}'.format(exc))
        else:
            _record(destroy_id, status='succeeded', deleted=result['deleted'],
                    skipped=result['skipped'], kept=result['kept'],
                    follow_up=result['follow_up'],
                    cluster_deleted=result.get('cluster_deleted'))
        finally:
            with _DEPLOY_LOCK:
                if _RUNNING_LINKS.get(name) == destroy_id:
                    del _RUNNING_LINKS[name]

    threading.Thread(target=worker, name=destroy_id, daemon=True).start()
    return jsonify({'deleting': True, 'destroy_id': destroy_id, 'status': 'running',
                    'poll': '/api/links/deployments/{}'.format(destroy_id)}), 202


@links_bp.route('/repair', methods=['POST'])
def repair_endpoint():
    """What changed between two versions of a link, and what to do about it.

    Body: {"current": {...}, "desired": {...}, "apply": false}

    With `apply` false this changes nothing -- it is the review step, and it is
    the one that answers "can I add a table without rebuilding". With `apply`
    true the stack change is carried out; the signal and the cleanup are returned
    for the operator, because one writes to their database and the other deletes
    their cache keys.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send {"current": {...}, "desired": {...}}'}), 400
    current = body.get('current')
    desired = body.get('desired')
    if not isinstance(current, dict) or not isinstance(desired, dict):
        return jsonify({
            'error': 'both "current" and "desired" must be link config objects',
        }), 400

    for label, document in (('current', current), ('desired', desired)):
        found = problems_for(document)
        if found:
            return jsonify({
                'ok': False, 'stage': 'validate', 'which': label,
                'problems': [{'path': p.path, 'message': p.message}
                             for p in found],
            })

    try:
        diff_doc = diff(current, desired)
    except RepairError as exc:
        return jsonify({'ok': False, 'stage': 'diff', 'error': str(exc),
                        'hint': exc.hint})

    region = body.get('region')
    if not diff_doc['changed']:
        return jsonify({'ok': True, 'changed': False, 'diff': diff_doc,
                        'diff_summary': describe_diff(diff_doc),
                        'plan': {'severity': 'none', 'steps': [],
                                 'blocked': False, 'notes': ['nothing changed']},
                        'plan_summary': 'nothing to do'})

    # A change that cannot be applied in place is decided by the diff alone, so it
    # is answered BEFORE any AWS call. Resolving first would make the clearest
    # possible answer -- "this needs a new link" -- depend on a credential and a
    # round trip it has no use for, and would report a resolution failure instead
    # of the real verdict.
    if any(c['severity'] == 'recreate' for c in diff_doc['changes']):
        blocked_plan = repair_plan(diff_doc, {})
        return jsonify({
            'ok': True,
            'changed': True,
            'diff': diff_doc,
            'diff_summary': describe_diff(diff_doc),
            'plan': blocked_plan,
            'plan_summary': describe_repair(blocked_plan),
            'applied': False,
        })

    # Everything else needs the DESIRED resolution, for the qualified table names
    # a snapshot signal has to name and for the signalling collection.
    try:
        resolution = resolve_link(desired, region=region)
    except ResolutionError as exc:
        return jsonify({'ok': False, 'stage': 'resolve', 'error': str(exc),
                        'hint': exc.hint, 'diff': diff_doc,
                        'diff_summary': describe_diff(diff_doc)})

    plan_doc = repair_plan(diff_doc, resolution)
    payload = {
        'ok': True,
        'changed': True,
        'diff': diff_doc,
        'diff_summary': describe_diff(diff_doc),
        'plan': plan_doc,
        'plan_summary': describe_repair(plan_doc),
        'applied': False,
    }

    if not body.get('apply'):
        return jsonify(payload)

    if plan_doc.get('blocked'):
        # Refused rather than half-applied: a blocked change is one where some
        # steps would succeed and leave the link in a state matching neither
        # document.
        payload['ok'] = False
        payload['stage'] = 'blocked'
        payload['error'] = ('this change cannot be applied to the running link; '
                            'it needs a new link')
        return jsonify(payload)

    try:
        clients = DeployClients(region=region)
    except DeployError as exc:
        payload['ok'] = False
        payload['stage'] = 'clients'
        payload['error'] = str(exc)
        payload['hint'] = exc.hint
        return jsonify(payload)

    deployer = Deployer(clients, template_body=_template_body)
    try:
        outcome = apply_repair(plan_doc, resolution, deployer)
    except (RepairError, DeployError) as exc:
        payload['ok'] = False
        payload['stage'] = 'apply'
        payload['error'] = str(exc)
        payload['hint'] = getattr(exc, 'hint', None)
        return jsonify(payload)

    payload['applied'] = True
    payload['deployed'] = outcome['deployed']
    payload['outstanding'] = outcome['outstanding']
    return jsonify(payload)


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
    if any('FAILED' in s or s.startswith('ROLLBACK') for s in states):
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
    cdc = stacks.get('cdc')
    if cdc and cdc.get('stack'):
        wiring = _cdc_wiring(clients, cdc['stack'])
        if wiring:
            if with_live:
                rt = wiring['runtime']
                rt['services'] = _service_status(
                    clients, rt.get('cluster'),
                    [rt.get('debezium_service'), rt.get('sink_service')])
            item['components'] = wiring
    return item


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

    return normalize(doc), warnings


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
