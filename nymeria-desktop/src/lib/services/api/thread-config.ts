import type { ThreadShareDocument, ThreadShareImportResult, TemporaryToolEntry } from '$lib/types';
import { MCPApi } from './mcp';

/**
 * Normalize the backend `temporary_tools` map ({name: {enabled_at, expires_at}})
 * into the camelCase client shape. These are TTL'd bindings (e.g. Skill Kit
 * required_tools) that are active on the thread until they expire.
 */
function normalizeTemporaryTools(raw: unknown): Record<string, TemporaryToolEntry> {
  const out: Record<string, TemporaryToolEntry> = {};
  if (raw && typeof raw === 'object') {
    for (const [name, entry] of Object.entries(raw as Record<string, any>)) {
      out[name] = {
        enabledAt: entry?.enabled_at ?? null,
        expiresAt: entry?.expires_at ?? null,
      };
    }
  }
  return out;
}

export class ThreadConfigApi extends MCPApi {
  // =========================================================================
  // Thread Configuration
  // =========================================================================

  private _normalizeThreadConfig(data: any): import('$lib/types').ThreadConfig {
    return {
      threadId: data.thread_id,
      instructions: data.instructions ?? null,
      disabledTools: data.disabled_tools ?? [],
      enabledTools: data.enabled_tools ?? [],
      temporaryTools: normalizeTemporaryTools(data.temporary_tools),
      llmConfig: data.llm_config ?? null,
      activeLlmFallback: data.active_llm_fallback ? {
        provider: data.active_llm_fallback.provider,
        model: data.active_llm_fallback.model,
        sourceProvider: data.active_llm_fallback.source_provider,
        sourceModel: data.active_llm_fallback.source_model,
        holdSeconds: data.active_llm_fallback.hold_seconds,
        activatedAt: data.active_llm_fallback.activated_at,
        expiresAt: data.active_llm_fallback.expires_at,
        providerRoute: data.active_llm_fallback.provider_route ?? null,
        openaiApiMode: data.active_llm_fallback.openai_api_mode ?? null,
        reason: data.active_llm_fallback.reason ?? null,
        httpStatus: data.active_llm_fallback.http_status ?? null,
      } : null,
      memoryCharLimit: data.memory_char_limit ?? null,
      imageWindowSize: data.image_window_size ?? null,
      sequentialToolExecution: data.sequential_tool_execution ?? null,
      systemPrompt: data.system_prompt ?? null,
      callable: data.callable ?? false,
      callableName: data.callable_name ?? null,
      callableDescription: data.callable_description ?? null,
      callableMaxIterations: data.callable_max_iterations ?? null,
      callableTeamId: data.callable_team_id ?? null,
      callableTeamName: data.callable_team_name ?? null,
      enabledSkills: data.enabled_skills ?? [],
      disabledSkills: data.disabled_skills ?? [],
      injectTodosInPrompt: data.inject_todos_in_prompt ?? false,
      showAutonomousPrompts: data.show_autonomous_prompts ?? false,
      showPromptMetadata: data.show_prompt_metadata ?? false,
      telegramAutonomousDelivery: data.telegram_autonomous_delivery ?? 'full',
      inAppNotificationLevel: data.in_app_notification_level ?? 'notify_only',
      notificationProfile: data.notification_profile ?? null,
      dreaming: data.dreaming ? {
        enabled: data.dreaming.enabled ?? false,
        minIntervalHours: data.dreaming.min_interval_hours ?? 6,
        minIdleMinutes: data.dreaming.min_idle_minutes ?? 30,
        minTurnsSinceLast: data.dreaming.min_turns_since_last ?? 10,
        model: data.dreaming.model ?? null,
        systemPrompt: data.dreaming.system_prompt ?? null,
        kickoffPrompt: data.dreaming.kickoff_prompt ?? null,
        lastDreamAt: data.dreaming.last_dream_at ?? null,
        lastDreamThreadId: data.dreaming.last_dream_thread_id ?? null,
      } : null,
      shadowParentId: data.shadow_parent_id ?? null,
      hooksEnabled: data.hooks_enabled ?? null,
      hookOverrides: data.hook_overrides ?? {},
      createdAt: data.created_at ?? null,
      updatedAt: data.updated_at ?? null,
      hasCustomizations: data.has_customizations ?? false,
    };
  }

  async getThreadConfig(threadId: string): Promise<import('$lib/types').ThreadConfig> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/config`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return this._normalizeThreadConfig(data);
  }

  async updateThreadConfig(
    threadId: string,
    updates: import('$lib/types').ThreadConfigUpdateRequest
  ): Promise<import('$lib/types').ThreadConfig> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/config`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(updates),
    });

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to save thread settings'));
    }

    const data = await response.json();
    return this._normalizeThreadConfig(data);
  }

  private _normalizeThreadNotepad(data: any): import('$lib/types').ThreadNotepad {
    return {
      threadId: data.thread_id,
      content: data.content ?? '',
      charCount: data.char_count ?? 0,
      charLimit: data.char_limit ?? 0,
    };
  }

  /** Read a thread's persistent notepad (the agent's thread memory). */
  async getThreadNotepad(threadId: string): Promise<import('$lib/types').ThreadNotepad> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/notepad`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return this._normalizeThreadNotepad(await response.json());
  }

  /** Replace a thread's notepad content (blank clears it). */
  async updateThreadNotepad(
    threadId: string,
    content: string
  ): Promise<import('$lib/types').ThreadNotepad> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/notepad`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify({ content }),
    });

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to save the notepad'));
    }

    return this._normalizeThreadNotepad(await response.json());
  }

  async triggerThreadDream(
    threadId: string,
    request: import('$lib/types').ThreadDreamRequest = {}
  ): Promise<import('$lib/types').ThreadDreamResponse> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/dream`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to start dreaming'));
    }

    return response.json();
  }

  async getAgentTemplates(): Promise<import('$lib/types').AgentTemplate[]> {
    const response = await fetch(`${this.getBaseUrl()}/agents/templates`, {
      headers: this.getHeaders()
    });
    if (!response.ok) return [];
    const data = await response.json();
    return (data.templates ?? []).map((t: any) => ({
      name: t.name,
      description: t.description ?? '',
      systemPrompt: t.system_prompt ?? '',
      tools: t.tools ?? [],
      allowedTools: t.allowed_tools ?? [],
      requiredEnvVars: t.required_env_vars ?? [],
      llmProvider: t.llm_provider ?? null,
      llmModel: t.llm_model ?? null,
      llmTemperature: t.llm_temperature ?? null,
      llmMaxTokens: t.llm_max_tokens ?? null,
    }));
  }

  async listAgentThreads(): Promise<import('$lib/types').ThreadConfig[]> {
    const response = await fetch(`${this.getBaseUrl()}/agents/threads`, {
      headers: this.getHeaders()
    });
    if (!response.ok) return [];
    const data = await response.json();
    return (data.threads ?? []).map((t: any) => this._normalizeThreadConfig(t));
  }

  async createAgentThread(
    request: import('$lib/types').AgentThreadCreateRequest
  ): Promise<import('$lib/types').ThreadConfig> {
    const response = await fetch(`${this.getBaseUrl()}/agents/threads`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request),
    });
    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to create the agent thread'));
    }
    const data = await response.json();
    return this._normalizeThreadConfig(data);
  }

  async listThreadTeams(): Promise<import('$lib/types').ThreadTeamApi[]> {
    const response = await fetch(`${this.getBaseUrl()}/thread-teams`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load thread teams'));
    }
    const data = await response.json();
    return data.teams ?? [];
  }

  async createThreadTeam(
    request: { name: string; thread_ids: string[] }
  ): Promise<import('$lib/types').ThreadTeamApi> {
    const response = await fetch(`${this.getBaseUrl()}/thread-teams`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request),
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to create thread team'));
    }
    return response.json();
  }

  async updateThreadTeam(
    teamId: string,
    updates: { name?: string; thread_ids?: string[] }
  ): Promise<import('$lib/types').ThreadTeamApi> {
    const response = await fetch(`${this.getBaseUrl()}/thread-teams/${encodeURIComponent(teamId)}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(updates),
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to update thread team'));
    }
    return response.json();
  }

  async deleteThreadTeam(teamId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/thread-teams/${encodeURIComponent(teamId)}`, {
      method: 'DELETE',
      headers: this.getHeaders(),
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to delete thread team'));
    }
  }


  async deleteThreadConfig(threadId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/config`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to reset thread settings'));
    }
  }

  async exportThread(threadId: string): Promise<ThreadShareDocument> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/export`,
      { headers: this.getHeaders() }
    );

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to export the thread'));
    }

    return response.json();
  }

  /**
   * Developer/debugging: fetch the raw deserialized latest LangGraph checkpoint
   * for a thread. Unlike getThreadHistory, no display projection is applied, so
   * tool calls and standalone tool results from prior turns appear verbatim.
   * Gated in the UI behind the client-local developer-mode toggle.
   */
  async getThreadCheckpoint(threadId: string): Promise<unknown> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/checkpoint`,
      { headers: this.getHeaders() }
    );

    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load checkpoint'));
    }

    return response.json();
  }

  async importThread(document: ThreadShareDocument | Record<string, unknown>): Promise<ThreadShareImportResult> {
    const response = await fetch(`${this.getBaseUrl()}/threads/import`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(document),
    });

    if (!response.ok) {
      throw new Error(await this._extractError(response, 'Failed to import the thread'));
    }

    const data = await response.json();
    return {
      status: data.status ?? 'ok',
      threadId: data.thread_id,
      title: data.title ?? 'Imported Thread',
      config: this._normalizeThreadConfig(data.config ?? {}),
      warnings: data.warnings ?? [],
    };
  }
}
