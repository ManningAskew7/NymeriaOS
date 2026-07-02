import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkflowApproval, WorkflowRun, WorkflowStepEvent } from '$lib/types';

const mocks = vi.hoisted(() => ({
  getWorkflowApprovals: vi.fn(),
  getWorkflowRuns: vi.fn(),
  registerIdentityReloadHook: vi.fn(),
  resolveWorkflowApproval: vi.fn(),
}));

vi.mock('$lib/services/api.svelte', () => ({
  api: {
    getWorkflowApprovals: mocks.getWorkflowApprovals,
    getWorkflowRuns: mocks.getWorkflowRuns,
    resolveWorkflowApproval: mocks.resolveWorkflowApproval,
  },
}));

vi.mock('./config.svelte', () => ({
  registerIdentityReloadHook: mocks.registerIdentityReloadHook,
}));

import { createWorkflowsStore } from './workflows.svelte';

function approval(recordId: string): WorkflowApproval {
  return {
    record_id: recordId,
    workflow_id: 'site_monitor',
    origin: 'tool',
    user_id: 'u1',
    thread_id: 't1',
    prompt: 'Archive 12 emails?',
    resume_entrypoint: 'resume',
    created_at: '2026-07-01T00:00:00Z',
    expires_at: '2026-07-08T00:00:00Z',
  };
}

function run(runId: string): WorkflowRun {
  return {
    run_id: runId,
    workflow_id: 'site_monitor',
    user_id: 'u1',
    thread_id: 't1',
    status: 'ok',
    timestamp: '2026-07-01T00:00:00Z',
    envelope: { status: 'ok' },
    steps: [
      { step: 1, verb: 'nym.state.get', status: 'ok', duration_ms: 4, args: '', result: '' },
    ],
  };
}

function stepEvent(runId: string, step: number): WorkflowStepEvent {
  return {
    workflow_id: 'site_monitor',
    run_id: runId,
    step,
    verb: `nym.verb${step}`,
    status: 'ok',
    duration_ms: 10,
  };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('workflowsStore loading', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads approvals and runs together', async () => {
    mocks.getWorkflowApprovals.mockResolvedValue([approval('r1')]);
    mocks.getWorkflowRuns.mockResolvedValue([run('run1')]);

    const store = createWorkflowsStore();
    await store.loadWorkflows();

    expect(store.approvals).toHaveLength(1);
    expect(store.runs).toHaveLength(1);
    expect(store.pendingCount).toBe(1);
    expect(store.loaded).toBe(true);
    expect(store.error).toBeNull();
  });

  it('humanizes a load failure and still marks loaded', async () => {
    mocks.getWorkflowApprovals.mockRejectedValue(new Error('API error: 500'));
    mocks.getWorkflowRuns.mockResolvedValue([]);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const store = createWorkflowsStore();
    await store.loadWorkflows();

    expect(store.error).toBeTruthy();
    expect(store.loaded).toBe(true);
    expect(store.approvals).toHaveLength(0);
    errorSpy.mockRestore();
  });

  it('resets on identity reload', async () => {
    mocks.getWorkflowApprovals.mockResolvedValue([approval('r1')]);
    mocks.getWorkflowRuns.mockResolvedValue([run('run1')]);

    const store = createWorkflowsStore();
    const reloadHook =
      mocks.registerIdentityReloadHook.mock.calls.at(-1)?.[0] as () => void;
    await store.loadWorkflows();
    expect(store.approvals).toHaveLength(1);

    reloadHook();

    expect(store.approvals).toHaveLength(0);
    expect(store.runs).toHaveLength(0);
    expect(store.loaded).toBe(false);
  });
});

describe('workflowsStore approvals', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('drops a resolved approval from the pending list', async () => {
    mocks.getWorkflowApprovals.mockResolvedValue([approval('r1'), approval('r2')]);
    mocks.getWorkflowRuns.mockResolvedValue([]);
    mocks.resolveWorkflowApproval.mockResolvedValue({
      ok: true,
      record_id: 'r1',
      decision: 'approved',
      run_id: 'cont1',
      workflow_id: 'site_monitor',
    });

    const store = createWorkflowsStore();
    await store.loadWorkflows();
    const result = await store.resolveApproval('r1', true);

    expect(result.decision).toBe('approved');
    expect(store.approvals.map((a) => a.record_id)).toEqual(['r2']);
  });

  it('does not resurrect a resolved approval from a stale refresh', async () => {
    mocks.getWorkflowApprovals.mockResolvedValue([approval('r1'), approval('r2')]);
    mocks.getWorkflowRuns.mockResolvedValue([]);
    mocks.resolveWorkflowApproval.mockResolvedValue({
      ok: true,
      record_id: 'r1',
      decision: 'approved',
      run_id: 'cont1',
      workflow_id: 'site_monitor',
    });

    const store = createWorkflowsStore();
    await store.loadWorkflows();
    await store.resolveApproval('r1', true);

    // A refresh whose server snapshot predates the claim still returns r1;
    // the store must keep it filtered out.
    await store.refreshApprovals();

    expect(store.approvals.map((a) => a.record_id)).toEqual(['r2']);
  });

  it('keeps the row and rethrows when resolution fails', async () => {
    mocks.getWorkflowApprovals.mockResolvedValue([approval('r1')]);
    mocks.getWorkflowRuns.mockResolvedValue([]);
    mocks.resolveWorkflowApproval.mockRejectedValue(
      new Error('API error: 409 - already being resolved')
    );

    const store = createWorkflowsStore();
    await store.loadWorkflows();

    await expect(store.resolveApproval('r1', false)).rejects.toThrow('409');
    expect(store.approvals).toHaveLength(1);
  });
});

describe('workflowsStore live runs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sorts out-of-order steps and drops duplicate deliveries', () => {
    const store = createWorkflowsStore();

    store.noteStepEvent(stepEvent('runA', 2));
    store.noteStepEvent(stepEvent('runA', 1));
    store.noteStepEvent(stepEvent('runA', 2));

    expect(store.liveRuns).toHaveLength(1);
    expect(store.liveRuns[0].steps.map((s) => s.step)).toEqual([1, 2]);
  });

  it('caps the number of tracked live runs at the oldest end', () => {
    const store = createWorkflowsStore();

    for (let i = 0; i < 7; i += 1) {
      store.noteStepEvent(stepEvent(`run${i}`, 1));
    }

    expect(store.liveRuns).toHaveLength(5);
    expect(store.liveRuns[0].run_id).toBe('run2');
    expect(store.liveRuns[4].run_id).toBe('run6');
  });

  it('retires a finished run and refreshes the persisted feed', async () => {
    mocks.getWorkflowRuns.mockResolvedValue([run('runA')]);

    const store = createWorkflowsStore();
    store.noteStepEvent(stepEvent('runA', 1));
    expect(store.liveRuns).toHaveLength(1);

    store.noteRunFinished({ workflow_id: 'site_monitor', run_id: 'runA', status: 'ok' });
    await flush();

    expect(store.liveRuns).toHaveLength(0);
    expect(mocks.getWorkflowRuns).toHaveBeenCalledTimes(1);
    expect(store.runs.map((r) => r.run_id)).toEqual(['runA']);
  });
});
