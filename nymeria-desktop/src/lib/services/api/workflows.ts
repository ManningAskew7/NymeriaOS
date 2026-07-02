import type {
  WorkflowApproval,
  WorkflowApprovalResolveResult,
  WorkflowRun,
  WorkflowRunStep,
} from '$lib/types';
import { HooksApi } from './hooks';

export class WorkflowsApi extends HooksApi {
  // =========================================================================
  // Workflows API
  //
  // Dashboard read surfaces for the `nym` workflow runtime: pending
  // nym.approve suspensions, recent run records, and the owner-or-admin
  // resolve call. Identity comes from the bearer token (the backend scopes
  // non-admins to their own rows), so unlike hooks there is no user_id
  // parameter on any of these.
  // =========================================================================

  private workflowApprovalFromResponse(item: Record<string, unknown>): WorkflowApproval {
    return {
      record_id: item.record_id as string,
      workflow_id: (item.workflow_id as string) || '',
      origin: (item.origin as string) || '',
      user_id: (item.user_id as string) || '',
      thread_id: (item.thread_id as string) || '',
      prompt: (item.prompt as string) || '',
      resume_entrypoint: (item.resume_entrypoint as string) || '',
      created_at: (item.created_at as string) || '',
      expires_at: (item.expires_at as string) || '',
    };
  }

  private workflowRunFromResponse(item: Record<string, unknown>): WorkflowRun {
    const trace = (item.trace as Record<string, unknown>) || {};
    const steps = Array.isArray(trace.steps) ? (trace.steps as Record<string, unknown>[]) : [];
    return {
      run_id: item.run_id as string,
      workflow_id: (item.workflow_id as string) || '',
      user_id: (item.user_id as string) || '',
      thread_id: (item.thread_id as string) || '',
      status: (item.status as string) || '',
      timestamp: (item.timestamp as string) || '',
      envelope: (item.envelope as Record<string, unknown>) || {},
      steps: steps.map((s): WorkflowRunStep => ({
        step: (s.step as number) || 0,
        verb: (s.verb as string) || '',
        status: (s.status === 'error' ? 'error' : 'ok'),
        duration_ms: (s.duration_ms as number) || 0,
        args: (s.args as string) || '',
        result: (s.result as string) || '',
        error_kind: (s.error_kind as string) || undefined,
      })),
    };
  }

  async getWorkflowApprovals(): Promise<WorkflowApproval[]> {
    const response = await fetch(`${this.getBaseUrl()}/workflows/approvals`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return ((data?.approvals as Record<string, unknown>[]) || []).map(
      (item) => this.workflowApprovalFromResponse(item)
    );
  }

  async resolveWorkflowApproval(
    recordId: string,
    approved: boolean,
    note?: string
  ): Promise<WorkflowApprovalResolveResult> {
    const response = await fetch(
      `${this.getBaseUrl()}/workflows/approvals/${recordId}/resolve`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ approved, ...(note ? { note } : {}) })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return await response.json() as WorkflowApprovalResolveResult;
  }

  async getWorkflowRuns(limit: number = 20): Promise<WorkflowRun[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    const response = await fetch(`${this.getBaseUrl()}/workflows/runs?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return ((data?.runs as Record<string, unknown>[]) || []).map(
      (item) => this.workflowRunFromResponse(item)
    );
  }
}
