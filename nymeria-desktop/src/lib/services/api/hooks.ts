import type {
  Hook,
  HookApproval,
  HookCreatedBy,
  HookCreateRequest,
  HookExecution,
  HookScope,
  HookTemplate,
  HookTemplateInstallResult,
  HookTestResult,
  HookUpdateRequest,
} from '$lib/types';
import { TriggersApi } from './triggers';

export class HooksApi extends TriggersApi {
  // =========================================================================
  // Lifecycle Hooks API
  //
  // CRUD over per-user hook definitions, a dry-run render, the per-user
  // execution log, and the bundled-template catalog. Hooks fire in-process,
  // so (unlike triggers) there is no webhook/fire surface here.
  // =========================================================================

  private hookFromResponse(item: Record<string, unknown>): Hook {
    return {
      id: item.id as string,
      name: item.name as string,
      event: item.event as Hook['event'],
      action: item.action as Hook['action'],
      logic: (item.logic as Record<string, unknown>) || {},
      text: (item.text as string) || '',
      matcher: (item.matcher as string) ?? null,
      fire_conditions: (item.fire_conditions as Hook['fire_conditions']) ?? [],
      once: (item.once ?? false) as boolean,
      single_use: (item.single_use ?? false) as boolean,
      enabled: (item.enabled ?? true) as boolean,
      scope: (item.scope || 'thread') as HookScope,
      thread_id: (item.thread_id || '') as string,
      created_by: (item.created_by || 'user') as HookCreatedBy,
      created_at: item.created_at as string,
      updated_at: item.updated_at as string,
      // Built-in system definition (the turn-metadata hook): locked identity,
      // delete = reset. Components branch on this for edit/reset affordances.
      system: (item.system ?? false) as boolean,
    };
  }

  async getHooks(userId?: string): Promise<Hook[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/hooks?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return (data || []).map((item: Record<string, unknown>) => this.hookFromResponse(item));
  }

  async createHook(request: HookCreateRequest, userId?: string): Promise<Hook> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/hooks?${params}`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return this.hookFromResponse(await response.json());
  }

  async updateHook(
    hookId: string,
    request: HookUpdateRequest,
    userId?: string
  ): Promise<Hook> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/hooks/${hookId}?${params}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return this.hookFromResponse(await response.json());
  }

  async deleteHook(hookId: string, userId?: string): Promise<void> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/hooks/${hookId}?${params}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  /**
   * Recent hook executions, newest first (per-user diagnostic log, cap 200
   * server-side). `hookId` narrows to one hook's runs.
   */
  async getHookExecutions(hookId?: string, limit = 20): Promise<HookExecution[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (hookId) params.set('hook_id', hookId);
    const response = await fetch(`${this.getBaseUrl()}/hooks/executions?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return ((await response.json()) || []) as HookExecution[];
  }

  /** The bundled hook-template catalog (install via installHookTemplate). */
  async getHookTemplates(): Promise<HookTemplate[]> {
    const response = await fetch(`${this.getBaseUrl()}/hooks/templates`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return (data?.templates || []) as HookTemplate[];
  }

  /**
   * Install a bundled template as a real hook. Idempotent per (template,
   * scope binding): reinstalling returns the existing hook with
   * `created: false`. Scope/thread overrides are optional; the template's
   * own default binding applies when omitted.
   */
  async installHookTemplate(
    templateId: string,
    options?: { scope?: HookScope; threadId?: string }
  ): Promise<HookTemplateInstallResult> {
    const body: Record<string, unknown> = {};
    if (options?.scope) body.scope = options.scope;
    if (options?.threadId) body.thread_id = options.threadId;
    const response = await fetch(
      `${this.getBaseUrl()}/hooks/templates/${encodeURIComponent(templateId)}/install`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify(body)
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      created: (data?.created ?? true) as boolean,
      hook: this.hookFromResponse((data?.hook ?? {}) as Record<string, unknown>),
    };
  }

  async testHook(hookId: string, userId?: string): Promise<HookTestResult> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/hooks/${hookId}/test?${params}`, {
      method: 'POST',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return await response.json();
  }

  /**
   * Pending require_approval holds visible to the caller (admins see all,
   * everyone else their own). Identity comes from the auth headers.
   */
  async getHookApprovals(): Promise<HookApproval[]> {
    const response = await fetch(`${this.getBaseUrl()}/hooks/approvals`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return (data?.approvals || []) as HookApproval[];
  }

  /**
   * Approve or deny a held tool call. Backend returns 404 for a record the
   * caller may not resolve (owner-or-admin) and 409 when the hold already
   * ended (timed out, resolved elsewhere, or its turn died).
   */
  async resolveHookApproval(
    recordId: string,
    approved: boolean,
    note?: string
  ): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/hooks/approvals/${encodeURIComponent(recordId)}/resolve`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify(note ? { approved, note } : { approved })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }
  }
}
