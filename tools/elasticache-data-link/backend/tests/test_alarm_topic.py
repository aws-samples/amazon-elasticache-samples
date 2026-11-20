"""Jira story F105: the Pipeline Monitor says when a link's alarms reach nobody.

Run: cd backend && python3 tests/test_alarm_topic.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import alarm_topic  # noqa: E402

TOPIC = 'arn:aws:sns:us-east-2:111122223333:ecdl-shop-cdc-AlarmTopic-1A2B'


class FakeCfn(object):
    def __init__(self, outputs=None, error=None):
        self.outputs, self.error, self.calls = outputs or {}, error, 0

    def describe_stacks(self, StackName):
        self.calls += 1
        if self.error:
            raise Exception(self.error)
        return {'Stacks': [{'Outputs': [{'OutputKey': k, 'OutputValue': v}
                                        for k, v in self.outputs.items()]}]}


class FakeSns(object):
    def __init__(self, pages, error=None):
        self.pages, self.error, self.calls = pages, error, []

    def list_subscriptions_by_topic(self, TopicArn, NextToken=None):
        self.calls.append((TopicArn, NextToken))
        if self.error:
            raise Exception(self.error)
        index = int(NextToken or 0)
        page = {'Subscriptions': [{'SubscriptionArn': a} for a in self.pages[index]]}
        if index + 1 < len(self.pages):
            page['NextToken'] = str(index + 1)
        return page


def _confirmed(n):
    return '{}:{}'.format(TOPIC, n)


class TopicStatusTest(unittest.TestCase):
    def test_no_subscription_is_an_alert(self):
        s = alarm_topic.topic_status(FakeCfn({'AlarmTopicArn': TOPIC}), FakeSns([[]]), 'ecdl-shop-cdc')
        self.assertEqual(s['state'], 'unread')
        self.assertIn('reach nobody', s['alert'])
        self.assertIn(TOPIC, s['alert'])

    def test_an_unconfirmed_email_does_not_count(self):
        s = alarm_topic.topic_status(FakeCfn({'AlarmTopicArn': TOPIC}),
                                     FakeSns([[alarm_topic.PENDING]]), 'ecdl-shop-cdc')
        self.assertEqual((s['state'], s['confirmed'], s['pending']), ('unread', 0, 1))
        self.assertIn('1 waiting for the email to be confirmed', s['alert'])

    def test_a_confirmed_subscription_clears_it(self):
        s = alarm_topic.topic_status(FakeCfn({'AlarmTopicArn': TOPIC}),
                                     FakeSns([[alarm_topic.PENDING], [_confirmed(1)]]), 'ecdl-shop-cdc')
        self.assertEqual((s['state'], s['confirmed'], s['pending']), ('ok', 1, 1))
        self.assertNotIn('alert', s)

    def test_deleted_subscriptions_do_not_count(self):
        s = alarm_topic.topic_status(FakeCfn({'AlarmTopicArn': TOPIC}), FakeSns([['Deleted']]), 's')
        self.assertEqual(s['state'], 'unread')

    def test_alarms_off_means_no_topic_and_no_alert(self):
        s = alarm_topic.topic_status(FakeCfn({}), FakeSns([[]]), 'ecdl-shop-cdc')
        self.assertEqual(s, {'state': 'off'})

    def test_a_read_failure_is_unknown_not_an_alert(self):
        s = alarm_topic.topic_status(FakeCfn({'AlarmTopicArn': TOPIC}),
                                     FakeSns([[]], error='AccessDenied'), 'ecdl-shop-cdc')
        self.assertEqual(s['state'], 'unknown')
        self.assertNotIn('alert', s)
        self.assertIn('AccessDenied', s['error'])


class CacheTest(unittest.TestCase):
    def setUp(self):
        alarm_topic._cache.clear()
        self.cfn = FakeCfn({'AlarmTopicArn': TOPIC})
        self.sns = FakeSns([[]])
        self.clock = [1000.0]

    def _client(self, service):
        return {'cloudformation': self.cfn, 'sns': self.sns}[service]

    def _status(self, link='shop'):
        return alarm_topic.cached_topic_status(link, self._client, now=lambda: self.clock[0])

    def test_reads_once_per_period_per_link(self):
        self._status()
        self._status()
        self.assertEqual(len(self.sns.calls), 1)
        self.clock[0] += alarm_topic.CACHE_SECONDS + 1
        self._status()
        self.assertEqual(len(self.sns.calls), 2)

    def test_the_link_stack_is_the_cdc_stack(self):
        self.assertEqual(self._status()['topic_arn'], TOPIC)

    def test_no_link_selected(self):
        self.assertIsNone(self._status(link=None))
        self.assertEqual(self.cfn.calls, 0)

    def test_an_error_is_retried_on_the_next_poll(self):
        self.sns.error = 'Throttling'
        self.assertEqual(self._status()['state'], 'unknown')
        self.sns.error = None
        self.assertEqual(self._status()['state'], 'unread')


class DashboardWiringTest(unittest.TestCase):
    def test_the_dashboard_carries_the_status_and_the_banner(self):
        with open(os.path.join(os.path.dirname(HERE), 'monitor.py')) as handle:
            src = handle.read()
        body = src[src.index('def collect_dashboard('):]
        body = body[:body.index("out['collected_at'] = now_ms()")]
        self.assertIn('alarm_topic.cached_topic_status(CFG.watched_link(), aws)', body)
        self.assertIn("out['flow']['alarm_alert'] = topic['alert']", body)


if __name__ == '__main__':
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
