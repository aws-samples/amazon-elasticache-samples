"""Runs each link's sampled cache check every VerifyIntervalMinutes.

Review finding C1: after the first load nothing compared the cache with the
source again. The check itself is data-plane/bulk-loader/verify.py, run as the
loader stack's verify task definition (it has the drivers, the link's source
secret and TLS). This only decides when: every minute it looks at the deployed
loader stacks and starts the check of each one that is due, unless

  - its interval is 0 (a link that only loads once),
  - a deploy of the link is running (the cache is being rebuilt),
  - capture is paused (Debezium desired count 0), so the cache is behind by
    design, or
  - the previous check is still running.

It uses only calls the control plane already makes for loads (ListStacks,
DescribeStacks, DescribeServices, ListTasks, RunTask of a link's own task
definition in the link's cluster), so it needs no new permissions. The first
check of a link runs two minutes after the control plane sees it, later ones
every interval. ECDL_VERIFY_SCHEDULE=false turns it off.
"""
import os
import threading
import time

TICK_SECONDS = 60
FIRST_DELAY_SECONDS = 120
SERVING = ['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']


def _link_of(stack_name):
    if stack_name.startswith('ecdl-') and stack_name.endswith('-loader'):
        return stack_name[len('ecdl-'):-len('-loader')] or None
    return None


class Scheduler(object):
    def __init__(self, cloudformation, ecs, busy=lambda link: False, clock=time.time, log=print):
        self.cfn, self.ecs, self.busy, self.clock, self.log = cloudformation, ecs, busy, clock, log
        self.next_at = {}

    def loader_stacks(self):
        names, token = [], None
        while True:
            kwargs = {'StackStatusFilter': SERVING}
            if token:
                kwargs['NextToken'] = token
            page = self.cfn.list_stacks(**kwargs)
            names += [s['StackName'] for s in page.get('StackSummaries') or []
                      if _link_of(s.get('StackName') or '')]
            token = page.get('NextToken')
            if not token:
                return names

    def outputs(self, name):
        stacks = self.cfn.describe_stacks(StackName=name).get('Stacks') or []
        return {o['OutputKey']: o['OutputValue'] for o in (stacks[0].get('Outputs') if stacks else None) or []}

    def why_not(self, link, out):
        """Why the check of a due link is not started now, or None."""
        if self.busy(link):
            return 'a deploy of the link is running'
        cluster = out.get('ClusterName')
        services = self.ecs.describe_services(
            cluster=cluster, services=['ecdl-{}-cdc-debezium'.format(link)]).get('services') or []
        if services and int(services[0].get('desiredCount') or 0) == 0:
            return 'capture is paused'
        running = self.ecs.list_tasks(cluster=cluster, family='ecdl-{}-loader-verify'.format(link),
                                      desiredStatus='RUNNING').get('taskArns') or []
        if running:
            return 'the previous check is still running'
        return None

    def tick(self):
        now = self.clock()
        started = []
        for name in self.loader_stacks():
            link = _link_of(name)
            try:
                out = self.outputs(name)
                arn = out.get('VerifyTaskDefinitionArn')
                interval = int(out.get('VerifyIntervalMinutes') or 0)
                if not arn or interval <= 0:
                    self.next_at.pop(name, None)
                    continue
                if name not in self.next_at:
                    self.next_at[name] = now + FIRST_DELAY_SECONDS
                if now < self.next_at[name]:
                    continue
                why = self.why_not(link, out)
                if why:
                    self.log('[verify] {}: check not started ({}); trying again in a minute'.format(link, why))
                    continue
                self.ecs.run_task(
                    cluster=out['ClusterName'], taskDefinition=arn, launchType='FARGATE',
                    startedBy='ecdl-verify',
                    networkConfiguration={'awsvpcConfiguration': {
                        'subnets': [s for s in (out.get('Subnets') or '').split(',') if s],
                        'securityGroups': [out['TaskSecurityGroupId']],
                        'assignPublicIp': out.get('AssignPublicIp') or 'DISABLED'}})
                self.next_at[name] = now + interval * 60
                started.append(link)
                self.log('[verify] {}: cache check started; results in /ecs/{} and ECDL/Verify'.format(
                    link, name))
            except Exception as exc:                                 # noqa: BLE001
                self.log('[verify] {}: could not start the cache check ({}: {})'.format(
                    link, type(exc).__name__, exc))
        return started


def start(region=None, busy=lambda link: False):
    if (os.environ.get('ECDL_VERIFY_SCHEDULE') or 'true').strip().lower() in ('0', 'false', 'off', 'no'):
        print('[verify] cache checks are off (ECDL_VERIFY_SCHEDULE=false)')
        return None
    import boto3
    session = boto3.session.Session(region_name=region)
    scheduler = Scheduler(session.client('cloudformation'), session.client('ecs'), busy=busy)

    def loop():
        while True:
            try:
                scheduler.tick()
            except Exception as exc:                                 # noqa: BLE001
                print('[verify] could not list the links ({}: {})'.format(type(exc).__name__, exc))
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, name='verify-schedule', daemon=True).start()
    print('[verify] cache checks scheduled from each loader stack\'s VerifyIntervalMinutes')
    return scheduler
