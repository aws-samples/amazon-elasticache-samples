/*
 * Tests for the shared deployment view.
 *
 * This module exists because the display was written twice and the copies drifted:
 * the Links run history had four step states and collapsed repeated polls, while
 * the Deployments page still had done/not-done and a raw event dump. The same
 * deployment therefore looked like progress on one page and like a hundred
 * identical "stack_waiting" lines on the other. These tests check the behaviour in
 * the one place it now lives.
 */
import {
  currentStepDetail,
  humanDuration,
  stepState,
  summariseEvents,
} from './deploymentView';

const STEPS = ['broker', 'cdc', 'pause_capture', 'loader', 'run_load',
               'resume_capture'];

describe('step state', () => {
  it('marks the first incomplete step as running while the deploy runs', () => {
    const d = { status: 'running', steps: STEPS, completed: ['broker'] };
    expect(stepState(d, 'broker')).toBe('done');
    expect(stepState(d, 'cdc')).toBe('running');
    expect(stepState(d, 'loader')).toBe('pending');
  });

  it('marks nothing as running once the deploy has stopped', () => {
    // A finished-or-failed deployment has no step in progress, and showing one
    // would suggest work is still happening.
    const d = { status: 'succeeded', steps: STEPS, completed: STEPS };
    STEPS.forEach((s) => expect(stepState(d, s)).toBe('done'));

    const failed = { status: 'failed', steps: STEPS, completed: ['broker'],
                     failed_step: 'cdc' };
    expect(stepState(failed, 'cdc')).toBe('failed');
    expect(stepState(failed, 'loader')).toBe('pending');
  });

  it('distinguishes four states, not two', () => {
    // The original marked steps done or not-done, so six steps mid-deploy were
    // six identical rows.
    const d = { status: 'running', steps: STEPS, completed: ['broker', 'cdc'] };
    const states = new Set(STEPS.map((s) => stepState(d, s)));
    expect(states).toEqual(new Set(['done', 'running', 'pending']));
  });
});

describe('what the running step is doing', () => {
  it('names the stack and its state', () => {
    const d = {
      status: 'running', steps: STEPS, completed: [],
      events: [
        { event: 'stack_started', step: 'broker', stack: 'ecdl-x-broker' },
        { event: 'stack_waiting', step: 'broker', stack: 'ecdl-x-broker',
          state: 'CREATE_IN_PROGRESS' },
      ],
    };
    const detail = currentStepDetail(d, 'broker');
    expect(detail).toContain('ecdl-x-broker');
    expect(detail).toContain('CREATE_IN_PROGRESS');
  });

  it('reports elapsed time, which is what separates working from stuck', () => {
    const now = Date.now();
    const d = {
      status: 'running', steps: STEPS, completed: [],
      events: [
        { event: 'stack_started', step: 'broker', at: now - 260000 },
        { event: 'stack_waiting', step: 'broker', state: 'CREATE_IN_PROGRESS',
          at: now },
      ],
    };
    expect(currentStepDetail(d, 'broker')).toContain('for 4m 20s');
  });

  it('sets the expectation for the broker, so a long wait is not a hang', () => {
    const d = {
      status: 'running', steps: STEPS, completed: [],
      events: [{ event: 'stack_waiting', step: 'broker',
                 state: 'CREATE_IN_PROGRESS' }],
    };
    expect(currentStepDetail(d, 'broker'))
      .toContain('ElastiCache usually takes 5 to 10 minutes');
  });

  it('reports task counts while a service is coming up', () => {
    const d = {
      status: 'running', steps: STEPS, completed: ['broker', 'cdc'],
      events: [{ event: 'service_waiting', step: 'pause_capture',
                 service: 'ecdl-x-debezium', desired: 1, running: 0 }],
    };
    expect(currentStepDetail(d, 'pause_capture')).toContain('0 of 1 task(s)');
  });

  it('names the table being loaded', () => {
    const d = {
      status: 'running', steps: STEPS, completed: [],
      events: [{ event: 'load_waiting', step: 'run_load', table: 'products' }],
    };
    expect(currentStepDetail(d, 'run_load')).toContain('loading products');
  });

  it('says nothing when there is nothing to say', () => {
    expect(currentStepDetail({ events: [] }, 'broker')).toBeNull();
  });
});

describe('event log', () => {
  it('collapses repeated polls into one line with a count', () => {
    // 113 events, 100 of them identical, was the actual output.
    const events = [{ event: 'plan_started' }];
    for (let i = 0; i < 100; i += 1) {
      events.push({ event: 'stack_waiting', step: 'broker',
                    stack: 'ecdl-x-broker', state: 'CREATE_IN_PROGRESS' });
    }
    events.push({ event: 'stack_complete', step: 'broker' });
    const lines = summariseEvents(events);
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain('(x100)');
    // And the informative last event is still visible rather than pushed off.
    expect(lines[2]).toContain('stack_complete');
  });

  it('does not collapse events that differ', () => {
    const lines = summariseEvents([
      { event: 'stack_waiting', step: 'broker', state: 'CREATE_IN_PROGRESS' },
      { event: 'stack_waiting', step: 'cdc', state: 'CREATE_IN_PROGRESS' },
    ]);
    expect(lines).toHaveLength(2);
    expect(lines.every((l) => !l.includes('(x'))).toBe(true);
  });

  it('carries the fields worth reading', () => {
    const lines = summariseEvents([
      { event: 'stack_recycling', step: 'broker', stack: 'ecdl-x-broker',
        state: 'CREATE_FAILED', reason: 'holds nothing to preserve' },
    ]);
    expect(lines[0]).toContain('CREATE_FAILED');
    expect(lines[0]).toContain('holds nothing to preserve');
  });

  it('tolerates no events at all', () => {
    expect(summariseEvents(undefined)).toEqual([]);
    expect(summariseEvents([])).toEqual([]);
  });
});

describe('durations', () => {
  it('reads naturally at each scale', () => {
    expect(humanDuration(45)).toBe('45s');
    expect(humanDuration(60)).toBe('1m');
    expect(humanDuration(260)).toBe('4m 20s');
  });
});
