"""verify_schedule: when the control plane starts each link's cache check
(review finding C1)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import verify_schedule  # noqa: E402

OUTPUTS = {'VerifyTaskDefinitionArn': 'arn:aws:ecs:r:1:task-definition/ecdl-shop-loader-verify:3',
           'VerifyIntervalMinutes': '15', 'ClusterName': 'ecdl-shop', 'Subnets': 'subnet-a,subnet-b',
           'TaskSecurityGroupId': 'sg-1', 'AssignPublicIp': 'DISABLED'}


class Cfn(object):
    def __init__(self, stacks):
        self.stacks = stacks

    def list_stacks(self, **_):
        return {'StackSummaries': [{'StackName': n} for n in self.stacks]}

    def describe_stacks(self, StackName=None):
        return {'Stacks': [{'Outputs': [{'OutputKey': k, 'OutputValue': v}
                                        for k, v in self.stacks[StackName].items()]}]}


class Ecs(object):
    def __init__(self, desired=1, running=()):
        self.desired, self.running, self.runs = desired, list(running), []

    def describe_services(self, cluster=None, services=None):
        return {'services': [{'serviceName': services[0], 'desiredCount': self.desired}]}

    def list_tasks(self, **_):
        return {'taskArns': self.running}

    def run_task(self, **kwargs):
        self.runs.append(kwargs)
        return {'tasks': [{}]}


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SchedulerTest(unittest.TestCase):
    def make(self, stacks=None, ecs=None, busy=lambda link: False):
        self.clock, self.lines = Clock(), []
        self.ecs = ecs or Ecs()
        stacks = stacks if stacks is not None else {'ecdl-shop-loader': dict(OUTPUTS),
                                                    'ecdl-shop-cdc': {}, 'ecdl-control-plane': {}}
        return verify_schedule.Scheduler(Cfn(stacks), self.ecs, busy=busy, clock=self.clock,
                                         log=self.lines.append)

    def test_first_check_after_two_minutes_then_every_interval(self):
        s = self.make()
        self.assertEqual(s.tick(), [])
        self.clock.t += verify_schedule.FIRST_DELAY_SECONDS
        self.assertEqual(s.tick(), ['shop'])
        run = self.ecs.runs[0]
        self.assertEqual(run['taskDefinition'], OUTPUTS['VerifyTaskDefinitionArn'])
        self.assertEqual(run['cluster'], 'ecdl-shop')
        self.assertEqual(run['networkConfiguration']['awsvpcConfiguration']['subnets'],
                         ['subnet-a', 'subnet-b'])
        self.clock.t += 14 * 60
        self.assertEqual(s.tick(), [])
        self.clock.t += 60
        self.assertEqual(s.tick(), ['shop'])

    def test_not_while_deploying_paused_or_still_running(self):
        for kwargs, why in ((dict(busy=lambda link: link == 'shop'), 'a deploy'),
                            (dict(ecs=Ecs(desired=0)), 'capture is paused'),
                            (dict(ecs=Ecs(running=['t1'])), 'still running')):
            s = self.make(**kwargs)
            s.tick()
            self.clock.t += verify_schedule.FIRST_DELAY_SECONDS
            self.assertEqual(s.tick(), [], why)
            self.assertIn(why, ' '.join(self.lines))

    def test_interval_zero_or_no_check_definition(self):
        for out in (dict(OUTPUTS, VerifyIntervalMinutes='0'),
                    {k: v for k, v in OUTPUTS.items() if k != 'VerifyTaskDefinitionArn'}):
            s = self.make(stacks={'ecdl-shop-loader': out})
            s.tick()
            self.clock.t += 3600
            self.assertEqual(s.tick(), [])
            self.assertEqual(self.ecs.runs, [])

    def test_one_failing_link_does_not_stop_the_others(self):
        class Flaky(Cfn):
            def describe_stacks(self, StackName=None):
                if StackName == 'ecdl-bad-loader':
                    raise RuntimeError('throttled')
                return Cfn.describe_stacks(self, StackName)
        s = verify_schedule.Scheduler(
            Flaky({'ecdl-bad-loader': {}, 'ecdl-shop-loader': dict(OUTPUTS)}), Ecs(),
            clock=lambda: 10 ** 9, log=lambda line: None)
        s.next_at['ecdl-shop-loader'] = 0
        self.assertEqual(s.tick(), ['shop'])


if __name__ == '__main__':
    unittest.main()
