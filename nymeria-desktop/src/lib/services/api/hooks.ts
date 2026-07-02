import type {
  Hook,
  HookCreatedBy,
  HookCreateRequest,
  HookScope,
  HookTestResult,
  HookUpdateRequest,
} from '$lib/types';
import { TriggersApi } from './triggers';

export class HooksApi extends TriggersApi {
  // =========================================================================
  // Lifecycle Hooks API
  //
  // Pure CRUD over per-user hook definitions plus a dry-run render. Hooks fire
  // in-process, so (unlike triggers) there is no webhook/fire or execution
  // surface here.
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
      enabled: (item.enabled ?? true) as boolean,
      scope: (item.scope || 'thread') as HookScope,
      thread_id: (item.thread_id || '') as string,
      created_by: (item.created_by || 'user') as HookCreatedBy,
      created_at: item.created_at as string,
      updated_at: item.updated_at as string,
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
}
