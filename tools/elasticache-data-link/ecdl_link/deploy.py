"""Execute a deployment plan against CloudFormation, ECS and Secrets Manager.

The planner (provision.py) decides WHAT to do. This does it, in order, wiring each
stack's outputs into the parameters of the stacks that depend on it.

Clients are injectable for the same reason resolve.py's are: the interesting cases
here are a stack that already exists, a stack that failed, a load task that exited
non-zero, and a rollback -- and none of those should need an AWS account to test.

WHY THIS IS SYNCHRONOUS AND SLOW
A full deploy waits on ElastiCache, which takes minutes. That cannot be avoided,
so the design choice is where the waiting happens: this runs to completion and
reports progress through a callback, and the caller decides whether to run it in a
background thread (the control plane does) or in the foreground (the CLI does).
Pretending it is fast, by returning as soon as the API calls are accepted, would
mean "deployed" and "working" were different things with nothing to tell them apart.
"""

from __future__ import annotations

import re
import time

# CloudFormation accepts at most 51,200 bytes of inline TemplateBody. The link
# templates carry their reasoning as comments, which pushed the CDC template
# past that, so comments are stripped before sending (see compact_template).
TEMPLATE_BODY_LIMIT = 51200

_BLOCK_SCALAR = re.compile(r'(:|^\s*-|!\w+)\s*[|>][-+0-9]*\s*(#.*)?$')


def compact_template(body):
    """The template with full-line comments and blank lines removed.

    Lines inside a block scalar (`key: |`, `key: >`, `!Sub |` ...) are content,
    not comments, even when they start with `#` -- a UserData script's own
    comments, say -- so they are kept exactly. Only whole-line comments are
    removed; a `#` after a value on the same line is left alone, because inside a
    quoted string it would not be a comment at all. The result parses to the
    same document (test_deploy checks this for every template in infra/).
    """
    out = []
    block_indent = None
    for line in body.split('\n'):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(' '))
        if block_indent is not None:
            if stripped == '' or indent > block_indent:
                out.append(line)
                continue
            block_indent = None
        if stripped == '' or stripped.startswith('#'):
            continue
        out.append(line)
        if _BLOCK_SCALAR.search(line):
            block_indent = indent
    # Trailing blank lines inside a final block scalar are not significant.
    while out and not out[-1].strip():
        out.pop()
    return '\n'.join(out) + '\n'

# Terminal stack states, split by whether the requested change took effect.
#
# UPDATE_ROLLBACK_COMPLETE is a FAILURE here. It used to be in the OK list, on the
# grounds that the stack is still usable -- and it is, but it is running the
# PREVIOUS version, so the update the operator asked for did not happen. Reporting
# that as "deployed" meant a bad parameter rolled back silently and the dashboard
# said the new configuration was live. The stack stays updatable (it is not in
# _UNRECOVERABLE), so the next deploy tries again.
_OK_STATES = ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE')
_BAD_STATES = ('CREATE_FAILED', 'ROLLBACK_COMPLETE', 'ROLLBACK_FAILED',
               'DELETE_COMPLETE', 'DELETE_FAILED',
               'UPDATE_ROLLBACK_COMPLETE', 'UPDATE_ROLLBACK_FAILED',
               'IMPORT_ROLLBACK_COMPLETE')
_ROLLED_BACK_HINT = (
    'The update was rolled back, so the stack is still running its PREVIOUS '
    'configuration. Fix the cause and deploy again; the stack can be updated.')
# A stack in one of these cannot be updated and cannot be recovered by retrying:
# it has to be deleted first. Naming that is much more useful than the raw
# ValidationError CloudFormation returns.
_UNRECOVERABLE = ('ROLLBACK_COMPLETE', 'ROLLBACK_FAILED', 'CREATE_FAILED')

# What to do about a stack the deployer cannot fix by itself (Jira story F138). These
# used to surface as CloudFormation's raw ValidationError, or with no hint.
_CREATE_FAILED_HINT = (
    'The stack is left in CREATE_FAILED so its events can be read (OnFailure '
    'DO_NOTHING), and whatever it did create keeps running, and is billed, until '
    'it is deleted. Fix the cause and deploy again (a stack that failed to create '
    'is deleted and created again), or delete the link: `ecdl destroy --link {link}`, '
    'or Delete on the Deployments page.')
_ROLLBACK_FAILED_HINT = (
    'The rollback of an update failed, so the stack can be neither updated nor '
    'deleted until it is finished: aws cloudformation continue-update-rollback '
    '--stack-name {stack} (add --resources-to-skip <logical id> for a resource '
    'that cannot be rolled back; the stack events name it). Then deploy again.')
_DELETE_FAILED_HINT = (
    'An earlier delete of this stack failed, and a stack in DELETE_FAILED cannot '
    'be updated. The stack events name the resource that blocked the delete. Fix '
    'it and delete the link again (`ecdl destroy --link {link}`), or retain that '
    'resource: aws cloudformation delete-stack --stack-name {stack} '
    '--retain-resources <logical id>. Then deploy.')
# Stack states the deployer refuses to update, with what to do instead.
_STUCK_HINTS = {'UPDATE_ROLLBACK_FAILED': _ROLLBACK_FAILED_HINT,
                'DELETE_FAILED': _DELETE_FAILED_HINT}

# One table's load may run this long unless the link (advanced.loader.
# timeout_minutes, carried on the plan as load_timeout_seconds) or the caller
# (`ecdl apply --load-timeout`) says otherwise (Jira story F068).
DEFAULT_LOAD_TIMEOUT_SECONDS = 21600


# Whether the link's initial load finished, kept as a tag on the LOADER STACK
# so it outlives the process that ran the load: the CLI on a laptop and the
# control plane both read it. Set to `incomplete` with the loader stack step of
# any plan that loads (before the first table), and to `complete` once every
# table has loaded. A stack without the tag predates it and counts as complete.
# Without this a failed load was forgotten: the next plain deploy took the
# update path, loaded nothing and reported success.
LOAD_MARKER_TAG = 'ecdl:initial-load'
LOAD_INCOMPLETE = 'incomplete'
LOAD_COMPLETE = 'complete'


def load_marker(tags):
    """'incomplete', 'complete', or None (no marker: an older stack)."""
    for tag in tags or []:
        if tag.get('Key') == LOAD_MARKER_TAG:
            value = tag.get('Value')
            return value if value in (LOAD_INCOMPLETE, LOAD_COMPLETE) else None
    return None


class DeployError(Exception):
    def __init__(self, message, hint=None, step=None):
        super(DeployError, self).__init__(message)
        self.hint = hint
        self.step = step


class Clients(object):
    """The AWS clients a deploy needs. Injectable, so tests use fakes."""

    def __init__(self, cloudformation=None, ecs=None, session=None,
                 region=None, profile=None, logs=None):
        if session is None and (cloudformation is None or ecs is None):
            try:
                import boto3
            except ImportError:
                raise DeployError('boto3 is required to deploy',
                                  'pip install boto3')
            session = boto3.session.Session(region_name=region,
                                            profile_name=profile)
        self.cloudformation = (cloudformation if cloudformation is not None
                              else session.client('cloudformation'))
        self.ecs = ecs if ecs is not None else session.client('ecs')
        self._session = session
        self._region = region
        self._logs = logs

    def logs(self):
        """A CloudWatch Logs client: a failed load's last error (Jira story F084) and
        the log lines a teardown keeps (Jira story F140)."""
        if self._logs is None:
            if self._session is None:
                import boto3
                self._session = boto3.session.Session(region_name=self._region)
            self._logs = self._session.client('logs')
        return self._logs

    def secrets(self):
        """A Secrets Manager client, for the broker offset probe."""
        if self._session is None:
            import boto3
            self._session = boto3.session.Session(region_name=self._region)
        return self._session.client('secretsmanager')

    def elasticache(self):
        """An ElastiCache client, for the broker's final snapshot on teardown."""
        if self._session is None:
            import boto3
            self._session = boto3.session.Session(region_name=self._region)
        return self._session.client('elasticache')



def _noop(event):
    pass


def _int_or(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _is_pause(step):
    return (step.get('kind') == 'action'
            and step.get('action') == 'set_debezium_desired'
            and int(step.get('desired', 1)) == 0)


def _is_resume(step):
    return (step.get('kind') == 'action'
            and step.get('action') == 'set_debezium_desired'
            and int(step.get('desired', 0)) > 0)


def plan_pauses_capture(plan_doc):
    """True when the plan holds capture paused for a bulk load."""
    return any(_is_pause(s) for s in (plan_doc or {}).get('steps') or [])


def _first_capture_start(plan_doc):
    """The stack step whose creation starts capture with nothing after it to
    check it: a first deploy of a cdc-only link. initial_load_and_cdc is checked
    at its resume step, and an update at ensure_capture."""
    if plan_doc.get('for_update') or plan_doc.get('mode') != 'cdc':
        return None
    if any(_is_resume(s) or s.get('action') == 'ensure_capture'
           for s in plan_doc['steps']):
        return None
    cdc = next((s for s in plan_doc['steps']
                if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
    return cdc['id'] if cdc else None


def _default_broker_connect(host, port, password, ssl, timeout):
    import valkey  # imported lazily: the CLI must work without it
    return valkey.Valkey(host=host, port=int(port), password=password or None,
                         ssl=ssl, ssl_cert_reqs='required',
                         ssl_check_hostname=bool(ssl),
                         socket_connect_timeout=timeout, socket_timeout=timeout)


def broker_client(secrets_client, broker_outputs, connect=None, timeout=5):
    """A client for the link's broker, from the broker stack's outputs.

    None when the outputs name no endpoint. Connecting is lazy in the valkey
    package, so an unreachable broker shows on the first command.
    """
    broker = broker_outputs or {}
    host = broker.get('PrimaryEndpoint')
    if not host:
        return None
    password = None
    if broker.get('AuthSecretArn'):
        password = secrets_client.get_secret_value(
            SecretId=broker['AuthSecretArn'])['SecretString']
    return (connect or _default_broker_connect)(
        host, broker.get('Port') or 6379, password,
        str(broker.get('TransitEncryption', 'true')).lower() == 'true', timeout)


def broker_offset_probe(secrets_client, connect=None, timeout=5):
    """An offset_probe that reads Debezium's committed offset from the broker.

    Returns a callable for Deployer(offset_probe=...). The callable answers:
      True  -- the link's offset hash exists and is non-empty
      False -- the broker answered and there is no offset yet
      None  -- the broker could not be reached from this process (the CLI on a
               laptop, or a control plane without the monitor security group).
               The deployer then falls back to a fixed grace period.

    `connect(host, port, password, ssl, timeout)` returns a client with
    `hlen(key)` and `time()`; injectable for tests. The default uses the valkey
    package.

    The callable also has `.broker_time_ms(plan_doc, outputs)`: the broker's own
    clock in milliseconds (TIME), or None when it cannot be read. Loaded rows are
    versioned with it, in the same clock the broker uses for stream ids.
    """

    def _client(outputs):
        return broker_client(secrets_client, outputs.get('broker'), connect, timeout)

    def probe(plan_doc, outputs):
        cdc = next((s for s in plan_doc['steps']
                    if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
        key = ((cdc or {}).get('parameters') or {}).get('OffsetKey')
        if not key:
            return None
        try:
            client = _client(outputs)
            return None if client is None else client.hlen(key) > 0
        except Exception:  # noqa: BLE001
            return None

    def broker_time_ms(plan_doc, outputs):
        try:
            client = _client(outputs)
            if client is None:
                return None
            seconds, micros = client.time()
            return int(seconds) * 1000 + int(micros) // 1000
        except Exception:  # noqa: BLE001
            return None

    def heartbeat_ms(plan_doc, outputs):
        """Broker time (ms) of Debezium's newest heartbeat; 0 when it has written
        none, None when the broker cannot be read."""
        cdc = next((s for s in plan_doc['steps']
                    if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
        prefix = ((cdc or {}).get('parameters') or {}).get('TopicPrefix')
        if not prefix:
            return None
        try:
            client = _client(outputs)
            if client is None:
                return None
            last = client.xrevrange(heartbeat_stream(prefix), max='+', min='-', count=1)
        except Exception:  # noqa: BLE001
            return None
        if not last:
            return 0
        entry_id = last[0][0]
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode('ascii', 'replace')
        ms = str(entry_id).partition('-')[0]
        return int(ms) if ms.isdigit() else None

    def reset_position(plan_doc, outputs):
        """Delete the link's offset and schema history keys (Jira story F057).

        Returns how many of the two keys existed, or None when the broker
        cannot be read from here. Only called while Debezium is stopped.
        """
        cdc = next((s for s in plan_doc['steps']
                    if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
        params = (cdc or {}).get('parameters') or {}
        keys = [k for k in (params.get('OffsetKey'), params.get('SchemaHistoryKey')) if k]
        if len(keys) != 2:
            return None
        try:
            client = _client(outputs)
            return None if client is None else int(client.delete(*keys))
        except Exception:  # noqa: BLE001
            return None

    probe.broker_time_ms = broker_time_ms
    probe.heartbeat_ms = heartbeat_ms
    probe.reset_position = reset_position
    return probe


def heartbeat_stream(topic_prefix):
    """The broker stream Debezium writes its heartbeat to (one entry every
    heartbeat.interval.ms while it captures). Same name as the CDC template's
    CAPTURE_HEARTBEAT_STREAM."""
    return 'data:__debezium-heartbeat.{}'.format(topic_prefix)


class Deployer(object):
    """Walks a plan's steps, in order."""

    def __init__(self, clients, template_body, on_progress=None,
                 poll_seconds=10, timeout_seconds=2700, sleep=time.sleep,
                 load_timeout_seconds=None, offset_probe=None,
                 offset_wait_seconds=180, offset_grace_seconds=30,
                 allow_unverified_offset=False, template_url=None, role_arn=None,
                 capture_wait_seconds=600, reset_position=False, pg_slot_dropped=False):
        # template_body(filename) -> the template text. Injected rather than read
        # from a path so the control plane can serve templates from the image and
        # tests can supply strings.
        self.clients = clients
        self.template_body = template_body
        # template_url(filename) -> the S3 URL of the release's copy of that
        # template, and role_arn: the CloudFormation service role (decision D1).
        # With both, every stack call sends TemplateURL + RoleARN: the caller
        # needs no permission on the resources, only on the pinned templates
        # and the role. Without them (the CLI with an operator's own
        # credentials) templates are sent inline, as before.
        self.template_url = template_url
        self.role_arn = role_arn
        self.on_progress = on_progress or _noop
        self.poll_seconds = poll_seconds
        # Stacks and services. ElastiCache takes the longest, at 10-20 minutes.
        self.timeout_seconds = timeout_seconds
        # The bulk load has its own budget. It used to share the 45-minute stack
        # timeout, so a large table was stopped before it finished and reported as a failed
        # deploy although nothing was wrong with it. Six hours covers hundreds of
        # millions of rows at the measured 140-250K rows/s. None: the plan's
        # load_timeout_seconds (advanced.loader.timeout_minutes), else six hours.
        self.load_timeout_seconds = load_timeout_seconds
        # offset_probe(plan_doc, outputs) -> True (Debezium has committed a
        # position), False (not yet), None (cannot tell from here). See
        # _confirm_offset for why the pause must wait for it.
        self.offset_probe = offset_probe
        self.offset_wait_seconds = offset_wait_seconds
        self.offset_grace_seconds = offset_grace_seconds
        # When the broker cannot be read from here, pausing used to go ahead after
        # a fixed grace period -- a guess that Debezium had committed a position.
        # If it had not, every change made during the load was skipped. Now it
        # refuses unless the operator says they checked (ecdl apply
        # --offset-checked), in which case the grace period still applies.
        self.allow_unverified_offset = allow_unverified_offset
        self.sleep = sleep
        # LOAD_VERSION for this run: the time capture was paused, as a stream id.
        self.load_version = None
        # The load task currently running, as (cluster, task ARN, table). Capture
        # is never resumed while it may still be writing.
        self.active_load = None
        # How long a failure waits for a stopped load task to reach STOPPED.
        self.stop_wait_seconds = 300
        # After run(): 'running', 'resumed', 'paused' (resume refused or failed),
        # or None when the plan never paused capture.
        self.capture_state = None
        self.capture_message = None
        # After Debezium is started or resumed, how long to wait for a heartbeat
        # newer than the start (see _confirm_streaming). A start measured live
        # took about 2.5 minutes: image pull, JVM, schema history recovery.
        self.capture_wait_seconds = capture_wait_seconds
        # After run(): True when a fresh heartbeat proved Debezium was capturing,
        # False when the broker could not be read to check, None when nothing
        # was started.
        self.capture_verified = None
        # True once this run has seen the link's offset in the broker
        # (_confirm_offset, or the probe before an update).
        self.offset_seen = False
        # After run(): True when the CDC stack records OffsetCommitted=true,
        # False when recording it failed, None when it was not attempted.
        self.offset_recorded = None
        # Last describe_stacks entry per stack name, from _stack_state: the
        # tags an update has to carry over.
        self._described = {}
        # Whether this plan runs the load (it then marks the loader stack).
        self._plan_loads = False
        # Start capture again from the source's CURRENT position, for a link
        # whose stored position the source has purged (Jira story F057). Only with a plan
        # that pauses for a load; see _reset_position for the order and why.
        self.reset_position = reset_position
        # PostgreSQL: the operator confirms the link's replication slot was
        # dropped (it cannot be done from here; see reset_position_refusal).
        self.pg_slot_dropped = pg_slot_dropped
        # After run(): True when the loader stack was marked complete, False
        # when that update failed (the next plain deploy then asks for a
        # reload), None when the plan loaded nothing.
        self.load_recorded = None
        # True once Debezium's desired count has been set above zero and not
        # set back to zero since (Jira story F085). A resume step that fails after this
        # failed WAITING for the task, not resuming: running it again only
        # waited another timeout.
        self._resume_sent = False
        # True once every table of this run's load finished. After that a
        # failure needs no reload, and the hint must not ask for one.
        self._load_done = False
        self._link = None

    # ── public ────────────────────────────────────────────────────────────
    def run(self, plan_doc):
        """Execute every step. Returns a result document.

        CAPTURE IS ALWAYS RESUMED. If any step fails while capture is paused for
        the bulk load, the plan's own resume step is run before the error is
        raised. Leaving Debezium at zero tasks used to be the failure mode: the
        monitor reported it as an ordinary "paused", and once the source's binlog
        or WAL retention ran out the changes from that window were permanently lost.
        Resuming is safe -- the stored offset predates the load, so nothing is
        skipped -- and the only thing still wrong afterwards is the rows the load
        did not write, which the error says how to fix.
        """
        outputs = {}
        done = []
        paused = None    # the pause step, while capture is held at zero
        self._plan_loads = any(s.get('action') == 'run_load' for s in plan_doc['steps'])
        self._link = plan_doc['link']
        if self.load_timeout_seconds is None:
            self.load_timeout_seconds = (plan_doc.get('load_timeout_seconds')
                                         or DEFAULT_LOAD_TIMEOUT_SECONDS)
        self._emit('plan_started', link=plan_doc['link'], mode=plan_doc['mode'],
                   steps=[s['id'] for s in plan_doc['steps']])

        if self.reset_position:
            # Every refusal comes before anything is changed.
            self._check_reset(plan_doc)

        # The templates take an EXISTING cluster, so it is created first. Doing it
        # here rather than in a template means two links can share one cluster
        # without either owning it, and deleting a link's stacks does not delete a
        # cluster the other link is still using.
        self._ensure_cluster(plan_doc['cluster_name'])

        try:
            first_start = _first_capture_start(plan_doc)
            for step in plan_doc['steps']:
                try:
                    if step['kind'] == 'stack':
                        since = (self._broker_now(plan_doc, outputs)
                                 if step['id'] == first_start else None)
                        outputs[step['id']] = self._stack_step(
                            step, outputs, plan_doc['link'], plan_doc=plan_doc)
                        if step['id'] == first_start:
                            self._confirm_streaming(plan_doc, outputs, step['id'], since)
                    elif step['kind'] == 'action':
                        if _is_pause(step):
                            # A reset replaces the stored position, so whether
                            # one exists now does not matter; the new one is
                            # confirmed in _reset_position.
                            if not self.reset_position:
                                self._confirm_offset(plan_doc, outputs, step)
                            # Recorded BEFORE the call: a pause that times out
                            # waiting for zero tasks may still have taken effect.
                            paused = step
                        since = (self._broker_now(plan_doc, outputs)
                                 if _is_resume(step) else None)
                        self._action_step(step, plan_doc, outputs)
                        if _is_pause(step) and self.reset_position:
                            self._reset_position(plan_doc, outputs, step)
                        if _is_resume(step):
                            paused = None
                            self.capture_state = 'running'
                            # Outside the pause: capture IS resumed (desired 1)
                            # whatever this finds, so a failure here must not
                            # trigger the resume-after-failure path.
                            self._confirm_streaming(plan_doc, outputs, step['id'], since)
                    else:
                        raise DeployError(
                            'unknown step kind {!r}'.format(step['kind']),
                            step=step['id'])
                except DeployError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise DeployError(
                        'step {} failed: {}'.format(step['id'], exc), step=step['id'])
                done.append(step['id'])
            self._record_offset_committed(plan_doc, outputs)
        except BaseException as exc:
            # BaseException so a Ctrl-C in the CLI is handled too. A load task
            # that may still be running is stopped FIRST, and capture is resumed
            # only once ECS reports it STOPPED: resuming while the loader still
            # writes lets older rows overwrite newer changes and brings deleted
            # rows back.
            stopped, why = self._stop_active_load()
            if paused is not None:
                if self._resume_sent:
                    # Capture already has desired count 1: the failure came
                    # after the resume was sent (waiting for the task, or the
                    # reset's brief start). Sending it again changes nothing.
                    self._resume_already_sent(exc)
                elif stopped:
                    self._resume_after_failure(plan_doc, outputs, paused, exc)
                else:
                    self._refuse_resume(plan_doc, exc, why)
            raise

        self._emit('plan_complete', link=plan_doc['link'], steps=done)
        return {'link': plan_doc['link'], 'mode': plan_doc['mode'],
                'completed': done, 'outputs': outputs,
                'cluster_name': plan_doc['cluster_name'],
                'capture_verified': self.capture_verified,
                'offset_recorded': self.offset_recorded,
                'load_recorded': self.load_recorded}

    # ── is capture actually capturing? (Jira story F058) ─────────────────────────────
    def _broker_now(self, plan_doc, outputs):
        reader = getattr(self.offset_probe, 'broker_time_ms', None)
        if reader is None:
            return None
        try:
            return reader(plan_doc, outputs)
        except Exception:  # noqa: BLE001
            return None

    def _confirm_streaming(self, plan_doc, outputs, step_id, since_ms):
        """After Debezium is started or resumed, wait for a heartbeat newer
        than the start.

        ECS reporting the task RUNNING only means the JVM started. Debezium can
        then fail on missing grants, a purged log position or a missing
        replication slot, and restart again and again, while the deploy said
        "succeeded". Debezium writes a heartbeat to the broker every 10 s while
        it streams, so one stamped (by the broker clock) after the start proves
        capture is working. None arrives in capture_wait_seconds: the deploy
        fails, with capture left running. The broker cannot be read from here
        (the CLI on a laptop): the deploy goes ahead and says it is unverified.
        """
        reader = getattr(self.offset_probe, 'heartbeat_ms', None)
        unverified = ('the broker is not reachable from this deployer, so whether '
                      'Debezium is capturing could not be checked. Watch the '
                      'capture-stale alarm or the Debezium log group')
        if reader is None or since_ms is None:
            self.capture_verified = False
            self._emit('capture_unverified', step=step_id, reason=unverified)
            return
        waited, readable, newest = 0, False, None
        while True:
            try:
                newest = reader(plan_doc, outputs)
            except Exception:  # noqa: BLE001
                newest = None
            if newest is not None:
                readable = True
                if newest > since_ms:
                    self.capture_verified = True
                    self._emit('capture_streaming', step=step_id, waited=waited)
                    return
            if waited >= self.capture_wait_seconds:
                break
            self._emit('capture_waiting', step=step_id, waited=waited)
            self.sleep(self.poll_seconds)
            waited += (self.poll_seconds or 1)
        if not readable:
            self.capture_verified = False
            self._emit('capture_unverified', step=step_id, reason=unverified)
            return
        self.capture_verified = False
        raise DeployError(
            'Debezium is running but has written no heartbeat in the {}s since it '
            'was started, so it is not capturing changes'.format(self.capture_wait_seconds),
            'Check the Debezium log group: missing grants, a purged binlog or WAL '
            'position, or a missing replication slot stop it at startup. Capture '
            'is left running (desired count 1); deploy again once it is fixed.',
            step=step_id)

    # ── capture safety ────────────────────────────────────────────────────
    def _stop_active_load(self):
        """Stop this deploy's load task and wait until ECS reports it STOPPED.

        Returns (True, None) when no load task can still be writing, or
        (False, reason). It used to call stop_task, swallow any error and resume
        at once, so a load could run on for minutes (or hours, after a Ctrl-C
        or an expired session) under live capture.
        """
        if self.active_load is None:
            return True, None
        cluster, task_arn, table = self.active_load
        self._emit('load_stopping', table=table, task=task_arn)
        try:
            self.clients.ecs.stop_task(cluster=cluster, task=task_arn,
                                       reason='ecdl deploy stopped before resuming capture')
        except Exception as exc:  # noqa: BLE001
            if 'not found' not in str(exc).lower():
                return False, 'the load task could not be stopped ({})'.format(exc)
        waited = 0
        while True:
            try:
                tasks = self.clients.ecs.describe_tasks(
                    cluster=cluster, tasks=[task_arn]).get('tasks') or []
            except Exception as exc:  # noqa: BLE001
                return False, 'the load task could not be read ({})'.format(exc)
            if not tasks or tasks[0].get('lastStatus') == 'STOPPED':
                self.active_load = None
                self._emit('load_stopped', table=table, task=task_arn)
                return True, None
            if waited >= self.stop_wait_seconds:
                return False, 'the load task was still {} after {}s'.format(
                    tasks[0].get('lastStatus'), self.stop_wait_seconds)
            self.sleep(self.poll_seconds)
            waited += (self.poll_seconds or 1)

    def _refuse_resume(self, plan_doc, exc, why):
        """Leave capture paused: a load task may still be writing."""
        from .provision import stack_names
        family = stack_names(plan_doc['link'], plan_doc.get('prefix') or 'ecdl')['loader']
        self.capture_state = 'paused'
        self._emit('capture_left_paused', reason=why)
        message = (
            'CAPTURE IS STILL PAUSED, on purpose: {}. Resuming while the load '
            'still writes would let older rows overwrite newer changes. Stop the '
            'task and check that none is running (aws ecs list-tasks --cluster {} '
            '--family {} --desired-status RUNNING), then set the Debezium service '
            'desired count to 1. Changes are only safe while the source still '
            'retains its log.'.format(why, plan_doc['cluster_name'], family))
        if isinstance(exc, DeployError):
            exc.hint = '{} {}'.format(exc.hint, message) if exc.hint else message
            exc.capture_resumed = False
        self.capture_message = message

    def _resume_after_failure(self, plan_doc, outputs, paused, exc):
        """Run the plan's resume step after a failure; never raises.

        The original error is what the caller needs to see, so a failure to
        resume is attached to it (as a hint) rather than replacing it.
        """
        resume = next((s for s in plan_doc['steps'] if _is_resume(s)), None)
        if resume is None:
            resume = dict(paused, id='resume_capture', desired=1)
        self._emit('capture_resuming_after_failure', step=resume['id'],
                   failed_step=getattr(exc, 'step', None))
        try:
            self._action_step(resume, plan_doc, outputs)
        except Exception as resume_exc:  # noqa: BLE001
            self._emit('capture_resume_failed', step=resume['id'],
                       error=str(resume_exc))
            message = (
                'CAPTURE IS STILL PAUSED: resuming it after the failure also '
                'failed ({}). Set the Debezium service desired count to 1 now; '
                'changes are only safe while the source still retains its log.{}'
                .format(resume_exc, self._after_load_note()))
        else:
            self._emit('capture_resumed_after_failure', step=resume['id'])
            message = (
                'Capture was resumed, so no changes are lost.{}'.format(
                    self._after_load_note()))
        if isinstance(exc, DeployError):
            exc.hint = '{} {}'.format(exc.hint, message) if exc.hint else message
            exc.capture_resumed = 'STILL PAUSED' not in message
        self.capture_state = 'paused' if 'STILL PAUSED' in message else 'resumed'
        self.capture_message = message

    def _after_load_note(self):
        """What the failure means for the cached rows: nothing, when every
        table loaded; otherwise the load has to run again."""
        if self._load_done:
            return ' Every table loaded, so nothing needs reloading.'
        return (' The cache may be missing rows the load did not write: once the '
                'cause is fixed, run the load again with `ecdl apply --reload` '
                '(control plane: Update and reload all tables); it resumes where '
                'it stopped.')

    def _resume_already_sent(self, exc):
        """The failure came after capture's desired count was set back to 1.

        Nothing is paused by this deploy any more, so the resume is not sent
        again (Jira story F085): that used to wait a second full timeout for the same
        task and then say the load had to run again although it had finished.
        What is wrong is that ECS has not got a Debezium task running.
        """
        self.capture_state = 'resumed'
        self._emit('capture_resume_unconfirmed',
                   reason='the Debezium service desired count is already 1')
        message = (
            'Capture is not paused: the Debezium service desired count is 1. If '
            'no Debezium task is running, ECS cannot start one: the service\'s '
            'events and the Debezium log group say why.{}'.format(
                self._after_load_note()))
        if isinstance(exc, DeployError):
            exc.hint = '{} {}'.format(exc.hint, message) if exc.hint else message
            exc.capture_resumed = True
        self.capture_message = message

    def _confirm_offset(self, plan_doc, outputs, step):
        """Refuse to pause capture until Debezium has committed a position.

        Pausing is only safe because the stored offset predates the load: on
        resume, Debezium replays from it. If there is NO stored offset yet,
        schema_only/no_data starts from the CURRENT log position at resume, and
        every change made during the load is skipped. ECS reporting the task as
        running does not mean an offset has been written -- the heartbeat (10s)
        and offset flush (1s) have to run first -- so this waits for the offset
        itself.
        """
        if self.offset_probe is None:
            state = None
        else:
            waited = 0
            while True:
                try:
                    state = self.offset_probe(plan_doc, outputs)
                except Exception as exc:  # noqa: BLE001
                    self._emit('offset_probe_error', step=step['id'], error=str(exc))
                    state = None
                if state is not False or waited >= self.offset_wait_seconds:
                    break
                self._emit('offset_waiting', step=step['id'], waited=waited)
                self.sleep(self.poll_seconds)
                waited += (self.poll_seconds or 1)
            if state is False:
                raise DeployError(
                    'Debezium has not committed a log position after {}s, so '
                    'capture was NOT paused and the load was not started'.format(
                        self.offset_wait_seconds),
                    'Pausing now would make Debezium resume from the log position '
                    'at resume time and skip every change made during the load. '
                    'Check the Debezium log group for a connection or permission '
                    'error, then deploy again.',
                    step=step['id'])
        if state is True:
            self.offset_seen = True
            self._emit('offset_confirmed', step=step['id'])
            return
        # Cannot see the broker from here (the CLI on a laptop, or no monitor
        # security group).
        if not self.allow_unverified_offset:
            raise DeployError(
                'could not read Debezium\'s committed log position from the broker, '
                'so capture was NOT paused and the load was not started',
                'Pausing without a committed position would make Debezium resume '
                'from the log position at resume time and skip every change made '
                'during the load. Deploy from the control plane (it can read the '
                'broker), or check that the Debezium log shows a committed offset '
                'and run again with `ecdl apply --offset-checked`.',
                step=step['id'])
        self._emit('offset_unverified', step=step['id'],
                   grace_seconds=self.offset_grace_seconds,
                   reason='the broker is not reachable from this deployer, so the '
                          'committed offset could not be read; waiting for '
                          'several heartbeat intervals instead')
        self.sleep(self.offset_grace_seconds)

    # ── reset the capture position (Jira story F057) ─────────────────────────────────
    def _check_reset(self, plan_doc):
        """Refuse a reset that cannot be done safely, before changing anything."""
        refusal = reset_position_refusal(plan_doc, reload=True, reset_position=True,
                                         pg_slot_dropped=self.pg_slot_dropped)
        if refusal is not None:
            raise refusal
        reset = getattr(self.offset_probe, 'reset_position', None)
        broker = next((s for s in plan_doc['steps']
                       if s.get('kind') == 'stack' and s.get('id') == 'broker'), None)
        if broker is None or self._stack_state(broker['name']) is None:
            raise DeployError(
                'there is no deployed capture position to reset: the broker stack '
                'does not exist', 'Deploy without --reset-position.',
                step='reset_position')
        outputs = {'broker': self._stack_outputs(broker['name'])}
        state = None
        if reset is not None and outputs.get('broker'):
            try:
                state = self.offset_probe(plan_doc, outputs)
            except Exception:  # noqa: BLE001
                state = None
        if state is None:
            raise DeployError(
                'cannot reset the capture position: the broker that holds it cannot be '
                'read from here, so nothing was changed',
                'A reset deletes the stored position and has to see Debezium record a '
                'new one before the load starts, or changes made during the load are '
                'lost. --offset-checked does not cover this. Deploy from the control '
                'plane, which can read the broker ("reset_position": true with '
                '"reload": true).',
                step='reset_position')

    def _reset_position(self, plan_doc, outputs, pause):
        """Replace a purged stored position with the source's current one.

        THE ORDER MATTERS. Called with Debezium just paused, before the load:
          1. wait until every Debezium task has STOPPED: a task that is still
             shutting down flushes its offset, which would put the stale
             position back after step 2;
          2. delete the offset and schema history keys on the broker;
          3. resume Debezium briefly. With no stored position it records the
             source's CURRENT one (schema-only snapshot), and wait until the
             offset key exists again;
          4. pause it again (and wait for it to stop), and only then let the
             load read the source.
        Deleting the key and simply resuming after the load would start capture
        at the position at RESUME time, and every change made during the load
        would be lost. Recording the position first means the load reads data
        that is newer than it, and resuming replays everything after it.

        Changes between the purged position and now are gone either way; the
        load is what puts the cache right.
        """
        cdc = next(s for s in plan_doc['steps']
                   if s['kind'] == 'stack' and s['id'] == pause['stack'])
        service = '{}-debezium'.format(cdc['name'])
        cluster = plan_doc['cluster_name']
        self._wait_for_tasks_stopped(cluster, service, pause['id'])
        deleted = self.offset_probe.reset_position(plan_doc, outputs)
        if deleted is None or self.offset_probe(plan_doc, outputs) is not False:
            raise DeployError(
                'could not delete the stored capture position on the broker',
                'Nothing was loaded. Check the control plane can reach the broker, '
                'then deploy again.', step='reset_position')
        self._emit('position_reset', step='reset_position', deleted=deleted,
                   purpose='deleted the stored offset and schema history so Debezium '
                           'records the current source position')
        resume = dict(pause, id='reset_resume_capture', desired=1,
                      purpose='start Debezium briefly so it records the current '
                              'source position before the load reads the source')
        self._action_step(resume, plan_doc, outputs)
        # Strict: the reset already proved the broker readable, and no grace
        # period can stand in for seeing the new position. Debezium is starting
        # from nothing here (image, JVM, schema snapshot), so it gets the
        # capture start budget rather than the shorter offset one.
        saved = (self.allow_unverified_offset, self.offset_wait_seconds)
        self.allow_unverified_offset = False
        self.offset_wait_seconds = max(self.offset_wait_seconds, self.capture_wait_seconds)
        try:
            self._confirm_offset(plan_doc, outputs, resume)
        finally:
            self.allow_unverified_offset, self.offset_wait_seconds = saved
        again = dict(pause, id='reset_pause_capture',
                     purpose='pause capture again, at the new position, for the load')
        self._action_step(again, plan_doc, outputs)
        self._wait_for_tasks_stopped(cluster, service, again['id'])
        # Stamped again once Debezium has fully stopped, so nothing it wrote on
        # the way out is newer than the loaded rows.
        self._stamp_load_version(plan_doc, outputs, again)

    def _wait_for_tasks_stopped(self, cluster, service, step_id):
        """Wait until no task of the service is still shutting down.

        runningCount reaches 0 when a task leaves RUNNING, before its process
        has exited; Debezium writes its offset on the way out.
        """
        waited = 0
        while True:
            listed = self.clients.ecs.list_tasks(
                cluster=cluster, serviceName=service, desiredStatus='STOPPED')
            arns = listed.get('taskArns') or []
            tasks = (self.clients.ecs.describe_tasks(cluster=cluster, tasks=arns)
                     .get('tasks') or []) if arns else []
            alive = [t for t in tasks if t.get('lastStatus') != 'STOPPED']
            if not alive:
                return
            if waited >= self.stop_wait_seconds:
                raise DeployError(
                    'a Debezium task was still {} after {}s'.format(
                        alive[0].get('lastStatus'), self.stop_wait_seconds),
                    'The stored position was not changed and nothing was loaded.',
                    step=step_id)
            self._emit('service_waiting', step=step_id, service=service,
                       stopping=len(alive))
            self.sleep(self.poll_seconds)
            waited += (self.poll_seconds or 1)

    # ── steps ─────────────────────────────────────────────────────────────
    # ── has this link committed an offset? (Jira story F005) ─────────────────────────
    def _record_offset_committed(self, plan_doc, outputs):
        """Set the CDC stack's OffsetCommitted to true once this run has seen
        Debezium commit an offset (or capture, by a fresh heartbeat).

        With it, Debezium refuses to start when the broker has no offset for
        the link (wiped, replaced, OffsetKey changed) and, on PostgreSQL, when
        the replication slot is gone, instead of quietly starting at the
        current log position and skipping everything since. It cannot be
        recorded from inside the task, so the deployer records it here, and
        every update keeps it (keep_previous). A reload sends false again.

        Changing it replaces the Debezium task once (a new task definition),
        so the run checks the new task streams. Never while capture is paused:
        the stack update would also apply the stack's desired count.
        """
        cdc = next((s for s in plan_doc['steps']
                    if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
        if cdc is None or self.capture_state == 'paused':
            return
        if not (self.offset_seen or self.capture_verified):
            return
        try:
            described = self.clients.cloudformation.describe_stacks(StackName=cdc['name'])
            stacks = described.get('Stacks') or []
            current = {p.get('ParameterKey'): p.get('ParameterValue') for p in
                       ((stacks[0].get('Parameters') if stacks else None) or [])}
        except Exception as exc:  # noqa: BLE001
            self._offset_not_recorded(cdc, 'the stack could not be read ({})'.format(exc))
            return
        if current.get('OffsetCommitted') == 'true':
            self.offset_recorded = True
            return
        if 'OffsetCommitted' not in current:
            self._offset_not_recorded(cdc, 'the stack has no OffsetCommitted parameter')
            return
        if str(current.get('DebeziumDesiredCount')) == '0':
            self._offset_not_recorded(cdc, 'the stack holds Debezium at 0 tasks; updating it '
                                           'would stop capture')
            return
        params = [{'ParameterKey': k, 'UsePreviousValue': True}
                  for k in sorted(current) if k != 'OffsetCommitted']
        params.append({'ParameterKey': 'OffsetCommitted', 'ParameterValue': 'true'})
        tags = [{'Key': 'ecdl:link', 'Value': plan_doc['link']},
                {'Key': 'ecdl:component', 'Value': cdc['id']}]
        self._emit('offset_recording', step=cdc['id'], stack=cdc['name'])
        since = self._broker_now(plan_doc, outputs)
        try:
            self.clients.cloudformation.update_stack(
                StackName=cdc['name'], Parameters=params,
                Capabilities=cdc['capabilities'], Tags=tags, **self._template_source(cdc))
            self._wait_for_stack(cdc['name'], cdc['id'])
        except Exception as exc:  # noqa: BLE001
            if 'No updates are to be performed' not in str(exc):
                self._offset_not_recorded(cdc, str(exc))
                return
        self.offset_recorded = True
        self._emit('offset_recorded', step=cdc['id'], stack=cdc['name'])
        # The update replaced the Debezium task: it must stream again.
        self._confirm_streaming(plan_doc, outputs, cdc['id'], since)

    def _offset_not_recorded(self, cdc, why):
        """The deploy itself succeeded; only the protection is not on yet."""
        self.offset_recorded = False
        self._emit('offset_record_failed', step=cdc['id'], stack=cdc['name'], reason=why,
                   hint='Until it is recorded, a start without a stored offset is not '
                        'refused. Deploy again to retry.')

    def _offset_committed_on_update(self, step, cfn_params, plan_doc, outputs):
        """On an update that keeps OffsetCommitted: if the broker already holds
        the link's offset, send true in this update, so recording it does not
        cost a second update (and a second Debezium restart) at the end."""
        if ('OffsetCommitted' not in (step.get('keep_previous') or [])
                or plan_doc is None or self.offset_probe is None):
            return cfn_params
        try:
            seen = self.offset_probe(plan_doc, outputs)
        except Exception:  # noqa: BLE001
            seen = None
        if seen is not True:
            return cfn_params
        self.offset_seen = True
        return [{'ParameterKey': 'OffsetCommitted', 'ParameterValue': 'true'}
                if p['ParameterKey'] == 'OffsetCommitted' else p for p in cfn_params]

    def _stack_step(self, step, outputs, link=None, plan_doc=None):
        parameters = dict(step['parameters'])
        for name, (source_id, output_name) in (step.get('from_outputs') or {}).items():
            if source_id not in outputs:
                raise DeployError(
                    'step {} needs {} from the {} stack, which has not been '
                    'deployed'.format(step['id'], output_name, source_id),
                    step=step['id'])
            available = outputs[source_id]
            if output_name not in available:
                raise DeployError(
                    'the {} stack did not export {} (it exported: {})'.format(
                        source_id, output_name, ', '.join(sorted(available)) or 'nothing'),
                    'The template\'s Outputs and the plan\'s wiring have to agree.',
                    step=step['id'])
            parameters[name] = available[output_name]

        self._emit('stack_started', step=step['id'], stack=step['name'],
                   template=step['template'], purpose=step['purpose'])
        state = self._stack_state(step['name'])

        if state in _UNRECOVERABLE:
            # Deleted and re-created rather than refused. A stack in one of these states never
            # finished creating, so by CloudFormation's own definition it holds
            # nothing usable -- there is no decision to make about what might be
            # lost, and the only possible next step is to delete it and try again.
            # Making a person do that manually between every attempt turns one
            # mistake into two steps, and the first deploy of anything real tends
            # to take a few attempts.
            #
            # Deliberately narrow: ONLY the states that cannot be updated and
            # cannot be recovered. An UPDATE_ROLLBACK_COMPLETE stack is still
            # serving traffic from its previous version and is never touched here.
            self._emit('stack_recycling', step=step['id'], stack=step['name'],
                       state=state,
                       reason='a stack in {} never finished creating, so it holds '
                              'nothing to preserve'.format(state))
            try:
                self.clients.cloudformation.delete_stack(
                    StackName=step['name'], **self._role())
                self._wait_for_delete(step['name'], step['id'])
            except DeployError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DeployError(
                    'stack {} is in {} and could not be deleted: {}'.format(
                        step['name'], state, exc),
                    'Delete it manually and deploy again. A stack that failed to '
                    'create keeps no usable resources, so deleting it loses '
                    'nothing.',
                    step=step['id'])
            self._emit('stack_recycled', step=step['id'], stack=step['name'])
            state = None

        source = self._template_source(step)
        cfn_params = [{'ParameterKey': k, 'ParameterValue': v}
                      for k, v in sorted(parameters.items())]
        # Every stack is tagged with its link. This used to read step['link'],
        # which the planner never sets, so no stack was ever tagged and there was
        # no way to find a link's resources (or its cost) by tag.
        link = step.get('link') or link
        tags = [{'Key': 'ecdl:link', 'Value': link},
                {'Key': 'ecdl:component', 'Value': step['id']}] if link else []
        # The load marker (LOAD_MARKER_TAG). A plan that loads sets it to
        # incomplete here, before the first table. Any other update carries the
        # existing value over: UpdateStack replaces the whole tag set, so
        # leaving it out would erase a failed load's marker.
        if step['id'] == 'loader' and self._plan_loads:
            tags.append({'Key': LOAD_MARKER_TAG, 'Value': LOAD_INCOMPLETE})
            self._emit('load_marked', step=step['id'], stack=step['name'],
                       state=LOAD_INCOMPLETE)
        elif state is not None:
            kept = load_marker((self._described.get(step['name']) or {}).get('Tags'))
            if kept:
                tags.append({'Key': LOAD_MARKER_TAG, 'Value': kept})

        if state in _STUCK_HINTS:
            raise DeployError(
                'stack {} is in {} and cannot be updated'.format(step['name'], state),
                _STUCK_HINTS[state].format(stack=step['name'], link=link or '<link>'),
                step=step['id'])

        if state is None:
            self.clients.cloudformation.create_stack(
                StackName=step['name'], Parameters=cfn_params,
                Capabilities=step['capabilities'], Tags=tags, OnFailure='DO_NOTHING',
                **source)
            self._emit('stack_creating', step=step['id'], stack=step['name'])
        else:
            cfn_params = self._keep_previous(step, cfn_params)
            cfn_params = self._keep_live_count(step, cfn_params, plan_doc)
            cfn_params = self._offset_committed_on_update(step, cfn_params, plan_doc, outputs)
            try:
                self.clients.cloudformation.update_stack(
                    StackName=step['name'], Parameters=cfn_params,
                    Capabilities=step['capabilities'], Tags=tags, **source)
                self._emit('stack_updating', step=step['id'], stack=step['name'])
            except Exception as exc:  # noqa: BLE001
                # "No updates are to be performed" is CloudFormation's way of
                # saying the stack already matches. That is success, not failure,
                # and treating it as an error would make redeploying an unchanged
                # link impossible.
                if 'No updates are to be performed' not in str(exc):
                    raise
                self._emit('stack_unchanged', step=step['id'], stack=step['name'])
                return self._stack_outputs(step['name'])

        self._wait_for_stack(step['name'], step['id'])
        collected = self._stack_outputs(step['name'])
        self._emit('stack_complete', step=step['id'], stack=step['name'],
                   outputs=sorted(collected))
        return collected

    def _role(self):
        return {'RoleARN': self.role_arn} if self.role_arn else {}

    def _template_source(self, step):
        """TemplateURL + RoleARN (D1), or the template inline."""
        if self.template_url and self.role_arn:
            return dict(TemplateURL=self.template_url(step['template']), RoleARN=self.role_arn)
        if self.template_url or self.role_arn:
            raise DeployError('a template URL and a deploy role go together; only one '
                              'is configured', step=step['id'])
        body = compact_template(self.template_body(step['template']))
        if len(body.encode('utf-8')) > TEMPLATE_BODY_LIMIT:
            raise DeployError(
                'template {} is {} bytes even without comments; CloudFormation '
                'accepts at most {} inline'.format(
                    step['template'], len(body.encode('utf-8')), TEMPLATE_BODY_LIMIT),
                'Split the template or deploy it from S3 (TemplateURL).',
                step=step['id'])
        return dict(TemplateBody=body)

    def _keep_previous(self, step, cfn_params):
        """On update, keep the stack's existing value for step['keep_previous'].

        If the existing stack has the parameter, it is sent as UsePreviousValue.
        If it predates the parameter, the parameter is sent EMPTY, so the
        template's own fallback applies -- which is what the stack was running
        with before the parameter existed.
        """
        keep = set(step.get('keep_previous') or [])
        # Kept only when the plan has no value: control-plane properties the CLI
        # on a laptop does not know. Sending '' dropped the broker's monitor
        # ingress and the task-role boundary on every CLI update.
        keep_if_empty = set(step.get('keep_if_empty') or [])
        if not keep and not keep_if_empty:
            return cfn_params
        described = self.clients.cloudformation.describe_stacks(StackName=step['name'])
        stacks = described.get('Stacks') or []
        existing = {p.get('ParameterKey') for p in
                    ((stacks[0].get('Parameters') if stacks else None) or [])}
        out = []
        for p in cfn_params:
            if (p['ParameterKey'] in keep_if_empty and not p.get('ParameterValue')
                    and p['ParameterKey'] in existing):
                out.append({'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True})
            elif p['ParameterKey'] not in keep:
                out.append(p)
            elif p['ParameterKey'] in existing:
                out.append({'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True})
            else:
                out.append({'ParameterKey': p['ParameterKey'], 'ParameterValue': ''})
        return out

    def _keep_live_count(self, step, cfn_params, plan_doc):
        """On update, send an autoscaled service's live desired count (Jira story F139).

        step['keep_live_count'] maps a parameter to the service it sizes
        ('<stack>-<suffix>'). ECS Application Auto Scaling moves that count, and
        an update that sent the planned value set it straight back, so a sink
        scaled out to drain a backlog dropped to 2 tasks on every deploy. The
        live count, kept within the planned min and max, makes the update leave
        it where it is. If it cannot be read, the stack's previous value is
        kept.
        """
        keep = step.get('keep_live_count') or {}
        if not keep or plan_doc is None:
            return cfn_params
        params = step.get('parameters') or {}
        existing = {p.get('ParameterKey') for p in
                    (self._described.get(step['name']) or {}).get('Parameters') or []}
        out = []
        for p in cfn_params:
            suffix = keep.get(p['ParameterKey'])
            if suffix is None or p.get('UsePreviousValue'):
                out.append(p)
                continue
            service = '{}-{}'.format(step['name'], suffix)
            live = None
            try:
                described = self.clients.ecs.describe_services(
                    cluster=plan_doc['cluster_name'], services=[service])
                items = [x for x in described.get('services') or []
                         if x.get('status') != 'INACTIVE']
                if items and items[0].get('desiredCount') is not None:
                    live = int(items[0]['desiredCount'])
            except Exception:  # noqa: BLE001
                live = None
            if live is None:
                if p['ParameterKey'] in existing:
                    out.append({'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True})
                    self._emit('live_count_unknown', step=step['id'], service=service,
                               parameter=p['ParameterKey'], kept='previous')
                else:
                    out.append(p)
                continue
            lo, hi = _int_or(params.get('SinkMinCount'), None), _int_or(params.get('SinkMaxCount'), None)
            if lo is not None:
                live = max(live, lo)
            if hi is not None:
                live = min(live, hi)
            out.append({'ParameterKey': p['ParameterKey'], 'ParameterValue': str(live)})
            self._emit('live_count_kept', step=step['id'], service=service,
                       parameter=p['ParameterKey'], value=live)
        return out

    def _action_step(self, step, plan_doc, outputs):
        if step['action'] == 'set_debezium_desired':
            # The service name is the template's: '${AWS::StackName}-debezium'.
            stack = next(s for s in plan_doc['steps']
                         if s['kind'] == 'stack' and s['id'] == step['stack'])
            service = '{}-debezium'.format(stack['name'])
            self._emit('action_started', step=step['id'], action=step['action'],
                       service=service, desired=step['desired'],
                       purpose=step['purpose'])
            self.clients.ecs.update_service(
                cluster=plan_doc['cluster_name'], service=service,
                desiredCount=step['desired'])
            self._resume_sent = int(step['desired']) > 0
            self._wait_for_service(plan_doc['cluster_name'], service,
                                   step['desired'], step['id'])
            if int(step['desired']) == 0:
                self._stamp_load_version(plan_doc, outputs, step)
            self._emit('action_complete', step=step['id'], action=step['action'])
            return

        if step['action'] == 'run_load':
            self._run_load(step, plan_doc, outputs)
            return
        if step['action'] == 'ensure_capture':
            self._ensure_capture(step, plan_doc, outputs)
            return

        raise DeployError('unknown action {!r}'.format(step['action']), step=step['id'])

    def _ensure_capture(self, step, plan_doc, outputs=None):
        """End of every update of a CDC link: capture must be running.

        An update never moves Debezium's desired count (DebeziumDesiredCount is
        kept), so an update can neither resume capture under a running load nor
        leave it paused while reporting success. A capture left at 0 -- a
        resume that failed, a control plane restarted mid-load, a pause by hand
        -- is resumed here, unless a load of this link is still running.
        """
        stack = next(s for s in plan_doc['steps']
                     if s['kind'] == 'stack' and s['id'] == step['stack'])
        service = '{}-debezium'.format(stack['name'])
        cluster = plan_doc['cluster_name']
        described = self.clients.ecs.describe_services(cluster=cluster, services=[service])
        services = [x for x in described.get('services') or [] if x.get('status') != 'INACTIVE']
        if not services:
            raise DeployError('service {} not found'.format(service), step=step['id'])
        outputs = outputs or {}
        since = self._broker_now(plan_doc, outputs)
        if int(services[0].get('desiredCount') or 0) > 0:
            self._emit('capture_running', step=step['id'], service=service)
            # The update may have replaced the Debezium task: check it streams.
            self._confirm_streaming(plan_doc, outputs, step['id'], since)
            return
        running = loader_tasks_running(self.clients.ecs, cluster, plan_doc)
        if running:
            raise DeployError(
                'capture is paused and a load of this link is still running ({} task(s)); '
                'not resuming under it'.format(len(running)),
                'Wait for the load to finish (or stop it), then apply again.',
                step=step['id'])
        self._emit('action_started', step=step['id'], action='ensure_capture',
                   service=service, desired=1,
                   purpose='capture was paused with no load running; resuming it')
        self.clients.ecs.update_service(cluster=cluster, service=service, desiredCount=1)
        self._wait_for_service(cluster, service, 1, step['id'])
        self._emit('capture_resumed', step=step['id'], service=service)
        self._confirm_streaming(plan_doc, outputs, step['id'], since)

    def _run_load(self, step, plan_doc, outputs):
        """Run the loader once per table and require every run to succeed.

        One run loads one table, so the tables are iterated here. A table whose
        load failed is not a partial success: the cache is missing rows that CDC
        will never send, because CDC only carries CHANGES.
        """
        loader = outputs.get('loader') or {}
        for name in ('TaskDefinitionArn', 'TaskSecurityGroupId', 'Subnets'):
            if name not in loader:
                raise DeployError(
                    'the loader stack did not export {}'.format(name), step=step['id'])
        tables = [t for t in (loader.get('Tables') or '').split(',') if t.strip()]
        if not tables:
            raise DeployError('the loader stack exported no tables to load',
                              step=step['id'])

        for table in tables:
            table = table.strip()
            self._require_capture_paused(plan_doc, step)
            self._emit('load_started', step=step['id'], table=table)
            started = self.clients.ecs.run_task(
                cluster=plan_doc['cluster_name'],
                taskDefinition=loader['TaskDefinitionArn'],
                launchType='FARGATE',
                networkConfiguration={'awsvpcConfiguration': {
                    'subnets': [s for s in loader['Subnets'].split(',') if s],
                    'securityGroups': [loader['TaskSecurityGroupId']],
                    'assignPublicIp': loader.get('AssignPublicIp', 'DISABLED'),
                }},
                overrides={'containerOverrides': [{
                    'name': 'loader',
                    'environment': self._load_environment(table),
                }]})
            failures = started.get('failures') or []
            if failures:
                raise DeployError(
                    'could not start the load task for {}: {}'.format(table, failures),
                    step=step['id'])
            arns = [t['taskArn'] for t in started.get('tasks') or []]
            if not arns:
                raise DeployError(
                    'the load task for {} did not start'.format(table), step=step['id'])
            self.active_load = (plan_doc['cluster_name'], arns[0], table)
            self._wait_for_task(plan_doc['cluster_name'], arns[0], table, step['id'],
                                log_group=loader.get('LogGroup'))
            self.active_load = None
            self._emit('load_complete', step=step['id'], table=table)
        self._load_done = True
        self._mark_load_complete(plan_doc, step)

    def _mark_load_complete(self, plan_doc, step):
        """Every table loaded: set the loader stack's marker to complete.

        A tags-only update of the loader stack: the same template (sent the
        same way as its stack step, so the control plane's pinned-template
        condition still holds) and every parameter kept. Tag changes update
        the resources in place. A failure here does not fail the deploy --
        the rows ARE loaded -- but it is reported, and the next plain deploy
        will ask for a reload because the marker still says incomplete.
        """
        loader = next((s for s in plan_doc['steps']
                       if s.get('kind') == 'stack' and s.get('id') == 'loader'), None)
        if loader is None:
            return
        try:
            described = (self.clients.cloudformation.describe_stacks(
                StackName=loader['name']).get('Stacks') or [{}])[0]
            tags = [t for t in described.get('Tags') or []
                    if t.get('Key') != LOAD_MARKER_TAG]
            tags.append({'Key': LOAD_MARKER_TAG, 'Value': LOAD_COMPLETE})
            params = [{'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True}
                      for p in described.get('Parameters') or []]
            try:
                self.clients.cloudformation.update_stack(
                    StackName=loader['name'], Parameters=params,
                    Capabilities=loader['capabilities'], Tags=tags,
                    **self._template_source(loader))
            except Exception as exc:  # noqa: BLE001
                if 'No updates are to be performed' not in str(exc):
                    raise
            else:
                self._wait_for_stack(loader['name'], step['id'])
        except Exception as exc:  # noqa: BLE001
            self.load_recorded = False
            self._emit('load_marker_failed', step=step['id'], stack=loader['name'],
                       error=str(exc),
                       reason='every table loaded, but the loader stack could not be '
                              'marked complete, so the next deploy will ask for a '
                              'reload')
            return
        self.load_recorded = True
        self._emit('load_marked', step=step['id'], stack=loader['name'],
                   state=LOAD_COMPLETE)

    def _require_capture_paused(self, plan_doc, step):
        """Before each table of a paused load: Debezium must still be at 0.

        Something else resuming capture mid-load (an update by another caller,
        a person) makes the next table load under live capture.
        """
        pause = next((s for s in plan_doc['steps'] if _is_pause(s)), None)
        if pause is None:
            return
        cdc = next(s for s in plan_doc['steps']
                   if s['kind'] == 'stack' and s['id'] == pause['stack'])
        service = '{}-debezium'.format(cdc['name'])
        # A task ECS means to run (desired status RUNNING) is capture coming back.
        listed = self.clients.ecs.list_tasks(
            cluster=plan_doc['cluster_name'], serviceName=service, desiredStatus='RUNNING')
        tasks = listed.get('taskArns') or []
        if tasks:
            raise DeployError(
                'capture is running again ({} Debezium task(s)) in the middle of the '
                'load; loading more would overwrite newer changes'.format(len(tasks)),
                'Something resumed capture during the load. Reload once nothing else '
                'is changing the link.', step=step['id'])

    def _stamp_load_version(self, plan_doc, outputs, step):
        """Record the version the loader gives every row it writes.

        Every change Debezium wrote to the broker before this moment is older
        than the rows the load is about to read, and every change written after
        capture resumes is newer. So loaded rows carry this moment as their
        version, in the sink's format (a stream id: broker milliseconds), and the
        sink's version guard does the rest: it skips the older changes instead
        of writing them over loaded data, and applies the newer ones.

        Read from the BROKER'S clock, after Debezium has stopped: stream ids come
        from that clock, so no clock difference between machines can make a
        later change look older than a loaded row. When the broker cannot be
        read, rows are written without a version, as before, and every change
        that arrives later applies over them.
        """
        reader = getattr(self.offset_probe, 'broker_time_ms', None)
        ms = None
        if reader is not None:
            try:
                ms = reader(plan_doc, outputs)
            except Exception:  # noqa: BLE001
                ms = None
        self.load_version = '{}-0'.format(int(ms)) if ms else None
        self._emit('load_version', step=step['id'], version=self.load_version,
                   note=None if ms else 'broker clock not readable: rows are loaded '
                                        'without a version')

    def _load_environment(self, table):
        # A replacing load: each row is rebuilt, rows the filter drops are
        # deleted, and keys of rows the source no longer has are swept after
        # the load (bulk_loader.sweep_stale). A merging load left fields a
        # transform had removed and every such row in the cache.
        env = [{'name': 'TABLE', 'value': table},
               {'name': 'REPLACE_EXISTING', 'value': 'true'}]
        if self.load_version:
            env.append({'name': 'LOAD_VERSION', 'value': self.load_version})
        return env

    # ── waiting ───────────────────────────────────────────────────────────
    def _wait_for_stack(self, name, step_id):
        deadline = self.timeout_seconds
        while deadline > 0:
            self.sleep(self.poll_seconds)
            # `or 1`: see _wait_for_delete. A zero interval must still advance.
            deadline -= (self.poll_seconds or 1)
            state = self._stack_state(name)
            if state is None:
                raise DeployError('stack {} disappeared while deploying'.format(name),
                                  step=step_id)
            if state in _OK_STATES:
                return state
            if state in _BAD_STATES:
                reason = self._failure_reason(name)
                hint = {'UPDATE_ROLLBACK_COMPLETE': _ROLLED_BACK_HINT,
                        'CREATE_FAILED': _CREATE_FAILED_HINT,
                        'UPDATE_ROLLBACK_FAILED': _ROLLBACK_FAILED_HINT}.get(state)
                if hint:
                    hint = hint.format(stack=name, link=self._link or '<link>')
                    reason = '{} {}'.format(reason, hint) if reason else hint
                raise DeployError(
                    'stack {} ended in {}'.format(name, state),
                    reason,
                    step=step_id)
            self._emit('stack_waiting', step=step_id, stack=name, state=state)
        raise DeployError(
            'stack {} did not finish within {}s'.format(name, self.timeout_seconds),
            'It may still be progressing; check the CloudFormation console.',
            step=step_id)

    def _wait_for_delete(self, name, step_id):
        """Wait for a stack to disappear, so the name is free to create again.

        Separate from _wait_for_stack because the terminal condition is inverted:
        here the stack going away IS success, whereas there it means something
        deleted the stack mid-deploy.
        """
        deadline = self.timeout_seconds
        while deadline > 0:
            state = self._stack_state(name)
            if state is None:
                return
            if state == 'DELETE_FAILED':
                raise DeployError(
                    'stack {} could not be deleted (DELETE_FAILED)'.format(name),
                    'Something in it is still in use or is protected. The '
                    'CloudFormation console names the resource that blocked the delete.',
                    step=step_id)
            self._emit('stack_deleting', step=step_id, stack=name, state=state)
            self.sleep(self.poll_seconds)
            # `or 1` so a zero poll interval still advances the deadline. Tests set
            # poll_seconds=0 to avoid real waiting, and subtracting zero from the
            # budget is an infinite loop -- a hang rather than a failure, which is
            # much harder to diagnose in a test.
            deadline -= (self.poll_seconds or 1)
        raise DeployError(
            'stack {} was still deleting after {}s'.format(
                name, self.timeout_seconds),
            'It may still be deleting; check the CloudFormation console and '
            'deploy again once it has gone.',
            step=step_id)

    def _wait_for_service(self, cluster, service, desired, step_id):
        deadline = self.timeout_seconds
        while deadline > 0:
            described = self.clients.ecs.describe_services(
                cluster=cluster, services=[service])
            items = described.get('services') or []
            if not items:
                raise DeployError('service {} not found'.format(service), step=step_id)
            running = items[0].get('runningCount', 0)
            if running == desired:
                return
            self._emit('service_waiting', step=step_id, service=service,
                       running=running, desired=desired)
            self.sleep(self.poll_seconds)
            deadline -= (self.poll_seconds or 1)
        raise DeployError(
            'service {} did not reach {} task(s) in time'.format(service, desired),
            step=step_id)

    def _load_log(self, task_arn, log_group):
        """(stream, last error line or None) of a load task's log.

        The task definition logs to <LogGroup>/loader/loader/<task id>. The
        last FATAL or ERROR line is what went wrong; a failed load used to
        say only "exited 1"."""
        stream = 'loader/loader/{}'.format(str(task_arn).rsplit('/', 1)[-1])
        if not log_group:
            return stream, None
        logs = None
        try:
            logs = self.clients.logs() if hasattr(self.clients, 'logs') else None
        except Exception:  # noqa: BLE001
            logs = None
        if logs is None:
            return stream, None
        last, token = None, None
        try:
            for _page in range(10):
                kwargs = {'logGroupName': log_group, 'logStreamNames': [stream],
                          'filterPattern': '?FATAL ?ERROR'}
                if token:
                    kwargs['nextToken'] = token
                page = logs.filter_log_events(**kwargs)
                for event in page.get('events') or []:
                    text = (event.get('message') or '').strip()
                    if text.startswith(('FATAL', 'ERROR', 'SystemExit: FATAL')):
                        last = text
                token = page.get('nextToken')
                if not token:
                    break
        except Exception:  # noqa: BLE001
            return stream, last
        if last and len(last) > 600:
            last = last[:600] + '...'
        return stream, last

    # What a load task's exit code means, for the error.
    _EXIT_MEANING = {
        1: 'the loader stopped on an error',
        78: 'the loader configuration is wrong (EX_CONFIG)',
        137: 'the container was killed: out of memory, or stopped after the '
             '30 s grace period',
        143: 'the loader was stopped (SIGTERM)',
    }

    def _wait_for_task(self, cluster, task_arn, table, step_id, log_group=None):
        # The load's own budget, not the stack one (see __init__).
        budget = self.load_timeout_seconds or DEFAULT_LOAD_TIMEOUT_SECONDS
        deadline = budget
        while deadline > 0:
            described = self.clients.ecs.describe_tasks(
                cluster=cluster, tasks=[task_arn])
            items = described.get('tasks') or []
            if not items:
                raise DeployError(
                    'the load task for {} vanished'.format(table), step=step_id)
            task = items[0]
            if task.get('lastStatus') == 'STOPPED':
                containers = task.get('containers') or []
                codes = [c.get('exitCode') for c in containers]
                # A missing exit code means the container never ran -- an image
                # pull failure, or a task the scheduler stopped. Treating that as
                # success would report a load that never happened.
                if not codes or codes[0] is None:
                    raise DeployError(
                        'the load task for {} stopped without running: {}'.format(
                            table, task.get('stoppedReason') or 'no reason given'),
                        step=step_id)
                if codes[0] != 0:
                    stream, last = self._load_log(task_arn, log_group)
                    reasons = [r for r in (containers[0].get('reason'),
                                           task.get('stoppedReason')) if r]
                    detail = '; '.join(
                        [self._EXIT_MEANING.get(codes[0], 'the loader failed')]
                        + reasons)
                    raise DeployError(
                        'the load of {} exited {} ({})'.format(table, codes[0], detail)
                        + (': {}'.format(last) if last else ''),
                        'The loader log is stream {} in {}. Once the cause is fixed, '
                        'run the load again with `ecdl apply --reload` (control '
                        'plane: Update and reload all tables); it resumes where it '
                        'stopped.'.format(stream, log_group or 'the link\'s loader '
                                                               'log group'),
                        step=step_id)
                return
            self._emit('load_waiting', step=step_id, table=table,
                       status=task.get('lastStatus'))
            self.sleep(self.poll_seconds)
            deadline -= (self.poll_seconds or 1)
        # The task is stopped, and waited for, by run()'s failure handler
        # (active_load is still set), before capture can be resumed.
        raise DeployError(
            'the load of {} did not finish within {}s'.format(table, budget),
            'Raise the load timeout for a very large table: '
            'advanced.loader.timeout_minutes in the link, or `ecdl apply '
            '--load-timeout MINUTES` for one run. The source must keep its log '
            'for longer than the load takes.',
            step=step_id)

    # ── helpers ───────────────────────────────────────────────────────────
    def _ensure_cluster(self, name):
        described = self.clients.ecs.describe_clusters(clusters=[name])
        for cluster in described.get('clusters') or []:
            if cluster.get('status') == 'ACTIVE':
                return
        self.clients.ecs.create_cluster(clusterName=name)
        self._emit('cluster_created', cluster=name)

    def _stack_state(self, name):
        """The stack's status, or None when it does not exist."""
        try:
            described = self.clients.cloudformation.describe_stacks(StackName=name)
        except Exception as exc:  # noqa: BLE001
            if 'does not exist' in str(exc):
                return None
            raise
        items = described.get('Stacks') or []
        self._described[name] = items[0] if items else None
        return items[0].get('StackStatus') if items else None

    def _stack_outputs(self, name):
        described = self.clients.cloudformation.describe_stacks(StackName=name)
        items = described.get('Stacks') or []
        if not items:
            return {}
        return {o['OutputKey']: o['OutputValue']
                for o in items[0].get('Outputs') or []}

    def _failure_reason(self, name):
        """The first genuinely failed event, which is the one worth reporting.

        A failed stack produces many events; the useful one is the
        earliest FAILED with a reason, not the summary that rolled it back.
        """
        try:
            events = self.clients.cloudformation.describe_stack_events(
                StackName=name).get('StackEvents') or []
        except Exception:  # noqa: BLE001
            return None
        for event in reversed(events):
            status = event.get('ResourceStatus') or ''
            reason = event.get('ResourceStatusReason') or ''
            if status.endswith('_FAILED') and reason and 'cancelled' not in reason.lower():
                return '{}: {}'.format(event.get('LogicalResourceId', '?'), reason)
        return None

    def _emit(self, event, **fields):
        payload = {'event': event}
        payload.update(fields)
        self.on_progress(payload)


# Stack states in which the stack is serving: deployed, from the guard's point of
# view. UPDATE_ROLLBACK_COMPLETE belongs here (unlike in _OK_STATES): the stack is
# live on its previous version, so redeploying updates it in place.
_SERVING_STATES = ('CREATE_COMPLETE', 'UPDATE_COMPLETE',
                   'UPDATE_ROLLBACK_COMPLETE', 'IMPORT_COMPLETE')


def stack_states(plan_doc, cloudformation, tags=None):
    """{stack_name: status or None} for every stack step in the plan.

    A describe error other than "does not exist" is raised, not ignored: the
    caller is deciding whether it is safe to deploy, and "could not tell" must
    not read as "nothing there". `tags`, a dict, receives each existing
    stack's tags from the same call.
    """
    states = {}
    for step in plan_doc.get('steps', []):
        if step.get('kind') != 'stack' or not step.get('name'):
            continue
        try:
            described = cloudformation.describe_stacks(StackName=step['name'])
        except Exception as exc:  # noqa: BLE001
            if 'does not exist' in str(exc):
                states[step['name']] = None
                continue
            raise
        items = described.get('Stacks') or []
        states[step['name']] = items[0].get('StackStatus') if items else None
        if tags is not None and items:
            tags[step['name']] = items[0].get('Tags') or []
    return states


def loader_tasks_running(ecs, cluster, plan_doc):
    """ARNs of this link's loader tasks that are running (family = the loader
    stack name). [] when the cluster does not exist."""
    from .provision import stack_names
    family = stack_names(plan_doc['link'], plan_doc.get('prefix') or 'ecdl')['loader']
    try:
        listed = ecs.list_tasks(cluster=cluster, family=family, desiredStatus='RUNNING')
    except Exception as exc:  # noqa: BLE001
        if 'ClusterNotFound' in str(exc) or 'not found' in str(exc).lower():
            return []
        raise
    return list(listed.get('taskArns') or [])


def deployment_state(plan_doc, cloudformation, ecs=None):
    """Classify what already exists for this plan's link.

      new         -- none of its stacks exist
      in_progress -- a stack is mid-operation, or (with `ecs`) a load of the link
                     is running: another deploy or a delete, from this process
                     or any other. Never deploy over it.
      deployed    -- every stack exists and is serving
      partial     -- some exist; an earlier first deploy did not finish

    Returns {'state': ..., 'stacks': {name: status}, 'loader_tasks': [...],
    'initial_load': the loader stack's load marker -- 'incomplete',
    'complete', or None when there is no loader stack or it has no marker}.
    """
    tags = {}
    states = stack_states(plan_doc, cloudformation, tags=tags)
    loader = next((s['name'] for s in plan_doc.get('steps', [])
                   if s.get('kind') == 'stack' and s.get('id') == 'loader'), None)
    initial_load = load_marker(tags.get(loader)) if loader else None
    present = {n: s for n, s in states.items() if s and s != 'DELETE_COMPLETE'}
    if any(s.endswith('_IN_PROGRESS') for s in present.values()):
        state = 'in_progress'
    elif not present:
        state = 'new'
    elif len(present) == len(states) and all(
            s in _SERVING_STATES for s in present.values()):
        state = 'deployed'
    else:
        state = 'partial'
    # A load holds capture paused while every stack reads CREATE/UPDATE_COMPLETE,
    # so by stacks alone the link looked deployed mid-load and another caller's
    # update ran under it. A running loader task of the link means in progress.
    loaders = []
    if ecs is not None and state in ('deployed', 'partial'):
        loaders = loader_tasks_running(ecs, plan_doc['cluster_name'], plan_doc)
        if loaders:
            state = 'in_progress'
    return {'state': state, 'stacks': present, 'loader_tasks': loaders,
            'initial_load': initial_load}


def wants_update_plan(plan_doc, state, reload=False):
    """Should a redeploy of this link be planned with for_update=True?

    Yes when the link is fully deployed AND runs capture: an update then changes
    the stacks only and does not change capture or the cached data. Re-running the
    first-deploy plan instead -- which is what the control plane used to do --
    paused capture and bulk-loaded every table again for a one-line change.

    No for a partial deploy (the load may never have run), for a load-only link
    (loading is the reason to run it again), and when the operator asks
    for a reload explicitly.
    """
    if reload or state != 'deployed':
        return False
    return any(s.get('kind') == 'stack' and s.get('id') == 'cdc'
               for s in plan_doc.get('steps', []))


def initial_load_refusal(plan_doc, current, reload=False):
    """A DeployError when a plain update would hide an unfinished load, else None.

    The loader stack's marker says the last load did not finish (it failed, was
    interrupted, or its completion could not be recorded). An update loads
    nothing, so applying one and reporting success would leave the cache
    missing rows that CDC never sends. The reload is not started on the
    operator's behalf: it pauses capture and reads every table again, which
    has to be asked for.
    """
    if current.get('initial_load') != LOAD_INCOMPLETE:
        return None
    if not wants_update_plan(plan_doc, current.get('state'), reload=reload):
        return None
    return DeployError(
        'the initial load of {!r} did not complete, so the cache may be missing '
        'rows; an update would not load them, so deploy it with a reload'.format(
            plan_doc['link']),
        'Deploy again with a reload (`ecdl apply --reload`, or "reload": true) to '
        'run the bulk load. The loader stack is tagged {}={} until a load '
        'finishes.'.format(LOAD_MARKER_TAG, LOAD_INCOMPLETE))


def reset_position_refusal(plan_doc, reload=False, reset_position=False,
                           pg_slot_dropped=False):
    """A DeployError when a requested position reset cannot go ahead, else None.

    Pure, so the CLI and the control plane refuse before starting. The deployer
    checks again, and also that the broker can be read (_check_reset).
    """
    if not reset_position:
        return None
    if not reload:
        return DeployError(
            'resetting the capture position needs a reload',
            'The changes between the purged position and now are gone, and only '
            'the bulk load puts them back. Use `ecdl apply --reload '
            '--reset-position`, or "reload": true with "reset_position": true.',
            step='reset_position')
    if plan_doc.get('for_update') or not plan_pauses_capture(plan_doc):
        return DeployError(
            'resetting the capture position needs a link that loads and captures '
            '(mode initial_load_and_cdc); this plan does not pause capture for a load',
            'Without a load, starting from the current position loses the changes '
            'since the purged one with nothing to restore them.',
            step='reset_position')
    cdc = next((s for s in plan_doc['steps']
                if s.get('kind') == 'stack' and s.get('id') == 'cdc'), {})
    params = cdc.get('parameters') or {}
    if params.get('SourceEngine') == 'postgresql' and not pg_slot_dropped:
        slot = params.get('PgSlotName') or 'ecdl_slot'
        return DeployError(
            'a PostgreSQL source also needs its replication slot recreated, which '
            'this deployer cannot do',
            'An invalidated slot cannot be restarted, and dropping it changes your '
            'database, so it is your step. 1) Stop capture: aws ecs update-service '
            '--cluster {c} --service {s} --desired-count 0. 2) On the source: '
            "SELECT pg_drop_replication_slot('{slot}'); (the slot named by the CDC "
            "stack's PgSlotName parameter; links deployed before per-link slots "
            'use ecdl_slot). 3) Deploy again with --pg-slot-dropped (or '
            '"pg_slot_dropped": true). Debezium creates a new slot at the current '
            'position when it starts.'.format(
                c=plan_doc.get('cluster_name'), s='{}-debezium'.format(cdc.get('name')),
                slot=slot),
            step='reset_position')
    return None


def add_load_step(plan_doc):
    """Append the run-the-loader action after the loader stack.

    Kept out of the planner: the planner describes desired state, and running a
    task is not state. It is added here so the plan a customer reviews still
    lists it.
    """
    if plan_doc.get('for_update'):
        # A change to a running link updates the stacks and then backfills by
        # signal. Re-running the bulk load here would re-read tables that did not
        # change and would need capture paused to be safe -- and it still would
        # not fix rows already cached under an old transform.
        return plan_doc
    steps = plan_doc['steps']
    index = next((i for i, s in enumerate(steps)
                  if s['kind'] == 'stack' and s['id'] == 'loader'), None)
    if index is None:
        return plan_doc
    if any(s.get('action') == 'run_load' for s in steps):
        return plan_doc
    # The loader stack is created before capture is paused (Jira story F086), so the load
    # goes after the pause: it is the one step that needs capture held at zero.
    pause = next((i for i, s in enumerate(steps) if _is_pause(s)), None)
    if pause is not None and pause > index:
        index = pause
    steps.insert(index + 1, {
        'kind': 'action',
        'id': 'run_load',
        'action': 'run_load',
        'purpose': 'run the load once per table and require every run to succeed',
        'depends_on': ['loader'] + ([steps[pause]['id']] if pause is not None else []),
    })
    return plan_doc
