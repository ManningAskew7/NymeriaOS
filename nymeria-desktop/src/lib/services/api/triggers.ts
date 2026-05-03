import type {
  Trigger,
  TriggerCreateRequest,
  TriggerCreatedBy,
  TriggerExecution,
  TriggerSourceInfo,
  TriggerTestResult,
  TriggerUpdateRequest
} from '$lib/types';
import { SkillsApi } from './skills';

export class TriggersApi extends SkillsApi {
  // =========================================================================
  // Triggers API
  // =========================================================================

  private triggerFromResponse(item: Record<string, unknown>): Trigger {
    return {
      id: item.id as string,
      name: item.name as string,
      source_type: item.source_type as string,
      source_config: (item.source_config as Record<string, unknown>) || {},
      action: item.action as Trigger['action'],
      conditions: (item.conditions as Trigger['conditions']) || [],
      enabled: (item.enabled ?? true) as boolean,
      cooldown_seconds: (item.cooldown_seconds || 0) as number,
      last_fired: (item.last_fired as string) || null,
      fire_count: (item.fire_count || 0) as number,
      thread_id: (item.thread_id || '') as string,
      created_at: item.created_at as string,
      created_by: (item.created_by || 'user') as TriggerCreatedBy,
      consecutive_errors: (item.consecutive_errors || 0) as number,
      last_error: (item.last_error as string) || null,
      health_status: (item.health_status || 'healthy') as Trigger['health_status'],
    };
  }

  async getTriggers(userId?: string): Promise<Trigger[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/triggers?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return (data || []).map((item: Record<string, unknown>) => this.triggerFromResponse(item));
  }

  async createTrigger(request: TriggerCreateRequest, userId?: string): Promise<Trigger> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/triggers?${params}`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.triggerFromResponse(data);
  }

  async updateTrigger(
    triggerId: string,
    request: TriggerUpdateRequest,
    userId?: string
  ): Promise<Trigger> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/triggers/${triggerId}?${params}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.triggerFromResponse(data);
  }

  async deleteTrigger(triggerId: string, userId?: string): Promise<void> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/triggers/${triggerId}?${params}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async getTriggerSources(): Promise<Record<string, TriggerSourceInfo>> {
    const response = await fetch(`${this.getBaseUrl()}/triggers/sources/list`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return data.sources || {};
  }

  async testTrigger(triggerId: string, userId?: string): Promise<TriggerTestResult> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId) });
    const response = await fetch(`${this.getBaseUrl()}/triggers/${triggerId}/test?${params}`, {
      method: 'POST',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return await response.json();
  }

  async getTriggerExecutions(
    triggerId: string,
    userId?: string,
    limit: number = 50
  ): Promise<TriggerExecution[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId), limit: limit.toString() });
    const response = await fetch(`${this.getBaseUrl()}/triggers/${triggerId}/executions?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return await response.json();
  }

  async getRecentTriggerExecutions(
    userId?: string,
    limit: number = 50
  ): Promise<TriggerExecution[]> {
    const params = new URLSearchParams({ user_id: this.resolveUserId(userId), limit: limit.toString() });
    const response = await fetch(`${this.getBaseUrl()}/triggers/executions/recent?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return await response.json();
  }
}
