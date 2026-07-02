/**
 * Workflows Store
 *
 * Reactive state for the workflow dashboard: pending nym.approve suspensions,
 * recent run records, and in-flight runs assembled from the workflow_step /
 * workflow_run_finished SSE events. Read-mostly; the only mutation is
 * resolving an approval (owner or admin).
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type {
  LiveWorkflowRun,
  WorkflowApproval,
  WorkflowApprovalResolveResult,
  WorkflowRun,
  WorkflowRunFinishedEvent,
  WorkflowStepEvent,
} from '$lib/types';

// Most in-flight runs tracked at once. A lost workflow_run_finished event
// must not leak live entries forever, so the oldest live run is dropped
// once the cap is hit (the persisted record still lands in the runs feed).
const MAX_LIVE_RUNS = 5;

export function createWorkflowsStore() {
  // State
  let approvals = $state<WorkflowApproval[]>([]);
  let runs = $state<WorkflowRun[]>([]);
  let liveRuns = $state<LiveWorkflowRun[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let identityGeneration = 0;

  // Latest-issued-wins per resource: an SSE-driven refresh must not be
  // overwritten by a slower fetch that was issued earlier.
  let approvalsFetchSeq = 0;
  let runsFetchSeq = 0;

  // Records this client resolved. A refresh whose server snapshot predates
  // the resolve would otherwise resurrect the row, and acting on it again
  // can only 404 (the record is claimed server-side).
  let resolvedRecordIds = new Set<string>();

  // Reset on account switch / sign-out — approvals and runs are per-user.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    approvals = [];
    runs = [];
    liveRuns = [];
    loading = false;
    loaded = false;
    error = null;
    resolvedRecordIds = new Set<string>();
  });

  function acceptApprovals(next: WorkflowApproval[]): void {
    approvals = next.filter((a) => !resolvedRecordIds.has(a.record_id));
  }

  // Actions
  async function loadWorkflows(): Promise<void> {
    if (loading) return;
    const requestGeneration = identityGeneration;
    const approvalsSeq = ++approvalsFetchSeq;
    const runsSeq = ++runsFetchSeq;
    loading = true;
    error = null;
    try {
      const [nextApprovals, nextRuns] = await Promise.all([
        api.getWorkflowApprovals(),
        api.getWorkflowRuns(),
      ]);
      if (requestGeneration !== identityGeneration) return;
      if (approvalsSeq === approvalsFetchSeq) acceptApprovals(nextApprovals);
      if (runsSeq === runsFetchSeq) runs = nextRuns;
      loaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your workflows' });
      console.error('Failed to load workflows:', e);
      // Mark loaded so consumer `$effect` blocks don't loop on a 404/auth error.
      loaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        loading = false;
      }
    }
  }

  /** Silent approvals refetch (SSE-driven; never disturbs the error state). */
  async function refreshApprovals(): Promise<void> {
    const requestGeneration = identityGeneration;
    const seq = ++approvalsFetchSeq;
    try {
      const nextApprovals = await api.getWorkflowApprovals();
      if (requestGeneration !== identityGeneration) return;
      if (seq !== approvalsFetchSeq) return;
      acceptApprovals(nextApprovals);
    } catch (e) {
      console.error('Failed to refresh workflow approvals:', e);
    }
  }

  /** Silent runs refetch (SSE-driven; never disturbs the error state). */
  async function refreshRuns(): Promise<void> {
    const requestGeneration = identityGeneration;
    const seq = ++runsFetchSeq;
    try {
      const nextRuns = await api.getWorkflowRuns();
      if (requestGeneration !== identityGeneration) return;
      if (seq !== runsFetchSeq) return;
      runs = nextRuns;
    } catch (e) {
      console.error('Failed to refresh workflow runs:', e);
    }
  }

  async function resolveApproval(
    recordId: string,
    approved: boolean,
    note?: string
  ): Promise<WorkflowApprovalResolveResult> {
    const result = await api.resolveWorkflowApproval(recordId, approved, note);
    // Resolved (either way) means no longer pending; drop the row and pin
    // the id so an in-flight stale refresh cannot resurrect it.
    resolvedRecordIds.add(recordId);
    approvals = approvals.filter((a) => a.record_id !== recordId);
    return result;
  }

  /** Fold one workflow_step SSE event into the live-runs view. */
  function noteStepEvent(event: WorkflowStepEvent): void {
    if (!event?.run_id) return;
    const existing = liveRuns.find((r) => r.run_id === event.run_id);
    if (existing) {
      // Steps can arrive out of order; keep them sorted and drop duplicates.
      if (existing.steps.some((s) => s.step === event.step)) return;
      const steps = [...existing.steps, event].sort((a, b) => a.step - b.step);
      liveRuns = liveRuns.map((r) =>
        r.run_id === event.run_id ? { ...r, steps } : r
      );
    } else {
      const next = [
        ...liveRuns,
        { run_id: event.run_id, workflow_id: event.workflow_id, steps: [event] },
      ];
      liveRuns = next.length > MAX_LIVE_RUNS ? next.slice(-MAX_LIVE_RUNS) : next;
    }
  }

  /** Fold one workflow_run_finished SSE event: retire the live entry and
   * pick up the persisted record it just produced. */
  function noteRunFinished(event: WorkflowRunFinishedEvent): void {
    if (!event?.run_id) return;
    liveRuns = liveRuns.filter((r) => r.run_id !== event.run_id);
    void refreshRuns();
  }

  function clearError(): void {
    error = null;
  }

  // Export store
  return {
    get approvals() { return approvals; },
    get runs() { return runs; },
    get liveRuns() { return liveRuns; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },

    get pendingCount() { return approvals.length; },

    loadWorkflows,
    refreshApprovals,
    refreshRuns,
    resolveApproval,
    noteStepEvent,
    noteRunFinished,
    clearError,
  };
}

export const workflowsStore = createWorkflowsStore();
