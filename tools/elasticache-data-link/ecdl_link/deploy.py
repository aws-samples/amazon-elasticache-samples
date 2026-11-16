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


class DeployError(Exception):
    def __init__(self, message, hint=None, step=None):
        super(DeployError, self).__init__(message)
        self.hint = hint
        self.step = step


class Clients(object):
    """The AWS clients a deploy needs. Injectable, so tests use fakes."""

    def __init__(self, cloudformation=None, ecs=None, session=None,
                 region=None, profile=None):
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

    def secrets(self):
        """A Secrets Manager client, for the broker offset probe."""
        if self._session is None:
            import boto3
            self._session = boto3.session.Session(region_name=self._region)
        return self._session.client('secretsmanager')


def _noop(event):
    pass


def _is_pause(step):
    return (step.get('kind') == 'action'
            and step.get('action') == 'set_debezium_desired'
            and int(step.get('desired', 1)) == 0)


def _is_resume(step):
    return (step.get('kind') == 'action'
            and step.get('action') == 'set_debezium_desired'
            and int(step.get('desired', 0)) > 0)


def broker_offset_probe(secrets_client, connect=None, timeout=5):
    """An offset_probe that reads Debezium's committed offset from the broker.

    Returns a callable for Deployer(offset_probe=...). The callable answers:
      True  -- the link's offset hash exists and is non-empty
      False -- the broker answered and there is no offset yet
      None  -- the broker could not be reached from this process (the CLI on a
               laptop, or a control plane without the monitor security group).
               The deployer then falls back to a fixed grace period.

    `connect(host, port, password, ssl, timeout)` returns a client with
    `hlen(key)`; injectable for tests. The default uses the valkey package.
    """
    def default_connect(host, port, password, ssl, timeout):
        import valkey  # imported lazily: the CLI must work without it
        return valkey.Valkey(host=host, port=int(port), password=password or None,
                             ssl=ssl, ssl_cert_reqs='required',
                             ssl_check_hostname=bool(ssl),
                             socket_connect_timeout=timeout, socket_timeout=timeout)

    connect = connect or default_connect

    def probe(plan_doc, outputs):
        broker = outputs.get('broker') or {}
        cdc = next((s for s in plan_doc['steps']
                    if s.get('kind') == 'stack' and s.get('id') == 'cdc'), None)
        key = ((cdc or {}).get('parameters') or {}).get('OffsetKey')
        host = broker.get('PrimaryEndpoint')
        if not host or not key:
            return None
        password = None
        if broker.get('AuthSecretArn'):
            password = secrets_client.get_secret_value(
                SecretId=broker['AuthSecretArn'])['SecretString']
        try:
            client = connect(host, broker.get('Port') or 6379, password,
                             str(broker.get('TransitEncryption', 'true')).lower()
                             == 'true', timeout)
            return client.hlen(key) > 0
        except Exception:  # noqa: BLE001
            return None

    return probe


class Deployer(object):
    """Walks a plan's steps, in order."""

    def __init__(self, clients, template_body, on_progress=None,
                 poll_seconds=10, timeout_seconds=2700, sleep=time.sleep,
                 load_timeout_seconds=21600, offset_probe=None,
                 offset_wait_seconds=180, offset_grace_seconds=30):
        # template_body(filename) -> the template text. Injected rather than read
        # from a path so the control plane can serve templates from the image and
        # tests can supply strings.
        self.clients = clients
        self.template_body = template_body
        self.on_progress = on_progress or _noop
        self.poll_seconds = poll_seconds
        # Stacks and services. ElastiCache takes the longest, at 10-20 minutes.
        self.timeout_seconds = timeout_seconds
        # The bulk load has its own budget. It used to share the 45-minute stack
        # timeout, so a large table was stopped before it finished and reported as a failed
        # deploy although nothing was wrong with it. Six hours covers hundreds of
        # millions of rows at the measured 140-250K rows/s.
        self.load_timeout_seconds = load_timeout_seconds
        # offset_probe(plan_doc, outputs) -> True (Debezium has committed a
        # position), False (not yet), None (cannot tell from here). See
        # _confirm_offset for why the pause must wait for it.
        self.offset_probe = offset_probe
        self.offset_wait_seconds = offset_wait_seconds
        self.offset_grace_seconds = offset_grace_seconds
        self.sleep = sleep

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
        self._emit('plan_started', link=plan_doc['link'], mode=plan_doc['mode'],
                   steps=[s['id'] for s in plan_doc['steps']])

        # The templates take an EXISTING cluster, so it is created first. Doing it
        # here rather than in a template means two links can share one cluster
        # without either owning it, and deleting a link's stacks does not delete a
        # cluster the other link is still using.
        self._ensure_cluster(plan_doc['cluster_name'])

        try:
            for step in plan_doc['steps']:
                try:
                    if step['kind'] == 'stack':
                        outputs[step['id']] = self._stack_step(
                            step, outputs, plan_doc['link'])
                    elif step['kind'] == 'action':
                        if _is_pause(step):
                            self._confirm_offset(plan_doc, outputs, step)
                            # Recorded BEFORE the call: a pause that times out
                            # waiting for zero tasks may still have taken effect.
                            paused = step
                        self._action_step(step, plan_doc, outputs)
                        if _is_resume(step):
                            paused = None
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
        except BaseException as exc:
            # BaseException so a Ctrl-C in the CLI resumes capture too.
            if paused is not None:
                self._resume_after_failure(plan_doc, outputs, paused, exc)
            raise

        self._emit('plan_complete', link=plan_doc['link'], steps=done)
        return {'link': plan_doc['link'], 'mode': plan_doc['mode'],
                'completed': done, 'outputs': outputs,
                'cluster_name': plan_doc['cluster_name']}

    # ── capture safety ────────────────────────────────────────────────────
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
                'changes are only safe while the source still retains its log.'
                .format(resume_exc))
        else:
            self._emit('capture_resumed_after_failure', step=resume['id'])
            message = (
                'Capture was resumed, so no changes are lost. The cache may be '
                'missing rows the load did not write: run the load again '
                '(`ecdl load` or redeploy with a reload) once the cause is fixed.')
        if isinstance(exc, DeployError):
            exc.hint = '{} {}'.format(exc.hint, message) if exc.hint else message
            exc.capture_resumed = 'STILL PAUSED' not in message

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
            self._emit('offset_confirmed', step=step['id'])
            return
        # Cannot see the broker from here (the CLI on a laptop, or no monitor
        # security group). Wait several heartbeat intervals instead, and say so.
        self._emit('offset_unverified', step=step['id'],
                   grace_seconds=self.offset_grace_seconds,
                   reason='the broker is not reachable from this deployer, so the '
                          'committed offset could not be read; waiting for '
                          'several heartbeat intervals instead')
        self.sleep(self.offset_grace_seconds)

    # ── steps ─────────────────────────────────────────────────────────────
    def _stack_step(self, step, outputs, link=None):
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
                self.clients.cloudformation.delete_stack(StackName=step['name'])
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

        body = compact_template(self.template_body(step['template']))
        if len(body.encode('utf-8')) > TEMPLATE_BODY_LIMIT:
            raise DeployError(
                'template {} is {} bytes even without comments; CloudFormation '
                'accepts at most {} inline'.format(
                    step['template'], len(body.encode('utf-8')), TEMPLATE_BODY_LIMIT),
                'Split the template or deploy it from S3 (TemplateURL).',
                step=step['id'])
        cfn_params = [{'ParameterKey': k, 'ParameterValue': v}
                      for k, v in sorted(parameters.items())]
        # Every stack is tagged with its link. This used to read step['link'],
        # which the planner never sets, so no stack was ever tagged and there was
        # no way to find a link's resources (or its cost) by tag.
        link = step.get('link') or link
        tags = [{'Key': 'ecdl:link', 'Value': link},
                {'Key': 'ecdl:component', 'Value': step['id']}] if link else []

        if state is None:
            self.clients.cloudformation.create_stack(
                StackName=step['name'], TemplateBody=body,
                Parameters=cfn_params, Capabilities=step['capabilities'],
                Tags=tags, OnFailure='DO_NOTHING')
            self._emit('stack_creating', step=step['id'], stack=step['name'])
        else:
            cfn_params = self._keep_previous(step, cfn_params)
            try:
                self.clients.cloudformation.update_stack(
                    StackName=step['name'], TemplateBody=body,
                    Parameters=cfn_params, Capabilities=step['capabilities'],
                    Tags=tags)
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

    def _keep_previous(self, step, cfn_params):
        """On update, keep the stack's existing value for step['keep_previous'].

        If the existing stack has the parameter, it is sent as UsePreviousValue.
        If it predates the parameter, the parameter is sent EMPTY, so the
        template's own fallback applies -- which is what the stack was running
        with before the parameter existed.
        """
        keep = set(step.get('keep_previous') or [])
        if not keep:
            return cfn_params
        described = self.clients.cloudformation.describe_stacks(StackName=step['name'])
        stacks = described.get('Stacks') or []
        existing = {p.get('ParameterKey') for p in
                    ((stacks[0].get('Parameters') if stacks else None) or [])}
        out = []
        for p in cfn_params:
            if p['ParameterKey'] not in keep:
                out.append(p)
            elif p['ParameterKey'] in existing:
                out.append({'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True})
            else:
                out.append({'ParameterKey': p['ParameterKey'], 'ParameterValue': ''})
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
            self._wait_for_service(plan_doc['cluster_name'], service,
                                   step['desired'], step['id'])
            self._emit('action_complete', step=step['id'], action=step['action'])
            return

        if step['action'] == 'run_load':
            self._run_load(step, plan_doc, outputs)
            return

        raise DeployError('unknown action {!r}'.format(step['action']), step=step['id'])

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
                    'environment': [{'name': 'TABLE', 'value': table}],
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
            self._wait_for_task(plan_doc['cluster_name'], arns[0], table, step['id'])
            self._emit('load_complete', step=step['id'], table=table)

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
                if state == 'UPDATE_ROLLBACK_COMPLETE':
                    reason = '{} {}'.format(reason, _ROLLED_BACK_HINT) if reason \
                        else _ROLLED_BACK_HINT
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

    def _wait_for_task(self, cluster, task_arn, table, step_id):
        # The load's own budget, not the stack one (see __init__).
        deadline = self.load_timeout_seconds
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
                    raise DeployError(
                        'the load of {} exited {}'.format(table, codes[0]),
                        'Check the loader log group for this link; the task '
                        'prints the table, engine and target it used on startup.',
                        step=step_id)
                return
            self._emit('load_waiting', step=step_id, table=table,
                       status=task.get('lastStatus'))
            self.sleep(self.poll_seconds)
            deadline -= (self.poll_seconds or 1)
        # Stop the task rather than leave it writing to the target after the
        # deploy has stopped waiting for it: a load still running when capture resumes
        # is exactly the stale-overwrite race the pause exists to prevent.
        try:
            self.clients.ecs.stop_task(cluster=cluster, task=task_arn,
                                       reason='ecdl deploy load timeout')
        except Exception:  # noqa: BLE001
            pass
        raise DeployError(
            'the load of {} did not finish within {}s and was stopped'.format(
                table, self.load_timeout_seconds),
            'Raise the load timeout for a very large table, or split it.',
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


def stack_states(plan_doc, cloudformation):
    """{stack_name: status or None} for every stack step in the plan.

    A describe error other than "does not exist" is raised, not ignored: the
    caller is deciding whether it is safe to deploy, and "could not tell" must
    not read as "nothing there".
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
    return states


def deployment_state(plan_doc, cloudformation):
    """Classify what already exists for this plan's link.

      new         -- none of its stacks exist
      in_progress -- a stack is mid-operation: another deploy (or a delete) is
                     running, from this process or any other. Never deploy over it.
      deployed    -- every stack exists and is serving
      partial     -- some exist; an earlier first deploy did not finish

    Returns {'state': ..., 'stacks': {name: status}}.
    """
    states = stack_states(plan_doc, cloudformation)
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
    return {'state': state, 'stacks': present}


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
    steps.insert(index + 1, {
        'kind': 'action',
        'id': 'run_load',
        'action': 'run_load',
        'purpose': 'run the load once per table and require every run to succeed',
        'depends_on': ['loader'],
    })
    return plan_doc
