"""Jira story F105: whether a link's alarms reach anyone.

The CDC stack publishes its alarms to its own SNS topic (output AlarmTopicArn),
which has a subscriber only when the link set advanced.alarms.email or someone
subscribed to it by hand. The guided wizard sets no email, so a link deployed
from the UI used to raise every alarm to nobody, silently. The plan warns about
that (ecdl_link.provision.alarm_topic_warning); this is the running check, for
the Pipeline Monitor's banner, and it also sees subscriptions made outside the
link. An email subscription counts only once it is confirmed: until then SNS
holds it as "PendingConfirmation" and delivers nothing.

Read at most every few minutes per stack: subscriptions change rarely, and the
dashboard polls every few seconds.
"""
import threading
import time

CACHE_SECONDS = 300
_cache = {}
_lock = threading.Lock()

PENDING = 'PendingConfirmation'


def _topic_arn(cfn, stack):
    described = cfn.describe_stacks(StackName=stack)['Stacks'][0]
    for output in described.get('Outputs') or []:
        if output.get('OutputKey') == 'AlarmTopicArn':
            return output.get('OutputValue')
    return None


def _subscriptions(sns, topic):
    """(confirmed, pending) subscription counts of a topic."""
    confirmed = pending = 0
    kwargs = {'TopicArn': topic}
    while True:
        page = sns.list_subscriptions_by_topic(**kwargs)
        for sub in page.get('Subscriptions') or []:
            arn = sub.get('SubscriptionArn') or ''
            if arn == PENDING:
                pending += 1
            elif arn.startswith('arn:'):
                confirmed += 1
        token = page.get('NextToken')
        if not token:
            return confirmed, pending
        kwargs['NextToken'] = token


def topic_status(cfn, sns, stack):
    """The alarm topic of one CDC stack, as the dashboard shows it.

    state: 'ok' (a confirmed subscriber), 'unread' (none; `alert` says so),
    'off' (the stack has no alarm topic: alarms disabled) or 'unknown'
    (could not be read; `error` says why). Never raises.
    """
    try:
        topic = _topic_arn(cfn, stack)
        if not topic:
            return {'state': 'off'}
        confirmed, pending = _subscriptions(sns, topic)
    except Exception as exc:  # noqa: BLE001 - shown, never fatal to the dashboard
        return {'state': 'unknown', 'error': str(exc)}
    out = {'state': 'ok' if confirmed else 'unread', 'topic_arn': topic,
           'confirmed': confirmed, 'pending': pending}
    if not confirmed:
        out['alert'] = (
            'Alarms reach nobody: the alarm topic has no confirmed subscription'
            + (' ({} waiting for the email to be confirmed)'.format(pending) if pending else '')
            + '. Subscribe an endpoint to {} or set advanced.alarms.email.'.format(topic))
    return out


def cached_topic_status(link_name, client, now=time.monotonic):
    """topic_status for a link, cached. `client(service)` makes AWS clients.
    None when no link is selected (the demo pipeline has no alarm topic)."""
    if not link_name:
        return None
    stack = 'ecdl-{}-cdc'.format(link_name)
    with _lock:
        hit = _cache.get(stack)
        if hit and now() - hit[0] < CACHE_SECONDS:
            return hit[1]
    status = topic_status(client('cloudformation'), client('sns'), stack)
    if status['state'] != 'unknown':          # retry an error on the next poll
        with _lock:
            _cache[stack] = (now(), status)
    return status
