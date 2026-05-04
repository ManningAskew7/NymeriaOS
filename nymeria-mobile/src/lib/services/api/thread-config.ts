import { MCPApi } from './mcp';

export class ThreadConfigApi extends MCPApi {
  private _normalizeThreadConfig(data: any): import('$lib/types').ThreadConfig {
    return {
      threadId: data.thread_id,
      instructions: data.instructions ?? null,
      disabledTools: data.disabled_tools ?? [],
      enabledTools: data.enabled_tools ?? [],
      llmConfig: data.llm_config ?? null,
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
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this._normalizeThreadConfig(data);
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
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }
    const data = await response.json();
    return this._normalizeThreadConfig(data);
  }
  async deleteThreadConfig(threadId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${threadId}/config`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  // =========================================================================
  // Triggers API
  // =========================================================================
}
