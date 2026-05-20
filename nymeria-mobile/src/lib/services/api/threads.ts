import type {
  AttachmentLimitsResponse,
  ContextStats,
  Message,
  MessageStep,
  ThreadHistory,
  ThreadStatus,
  ToolCall
} from '$lib/types';
import { configStore } from '$lib/stores/config.svelte';
import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
import { ChatApi } from './chat';

export class ThreadsApi extends ChatApi {
  async listThreads(): Promise<{ thread_id: string; platform: string }[]> {
    const response = await fetch(`${this.getBaseUrl()}/threads`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return data.threads || [];
  }

  /**
   * List all threads with full metadata (titles, pins, platform info).
   * This is the primary method for syncing thread state from the backend.
   */
  async listThreadsWithMetadata(): Promise<{
    threads: Array<{
      thread_id: string;
      title: string;
      pinned: boolean;
      platform: string;
      callable?: boolean;
      platform_meta: Record<string, string> | null;
      created_at: string | null;
      updated_at: string | null;
      title_source: string;
      recovered?: boolean;
      recovery_sources?: string[];
    }>;
    total: number;
  }> {
    const response = await fetch(`${this.getBaseUrl()}/threads`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  /**
   * Update thread metadata (title, pin status).
   */
  async updateThreadMetadata(
    threadId: string,
    updates: { title?: string; pinned?: boolean }
  ): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/metadata`,
      {
        method: 'PATCH',
        headers: this.getHeaders(),
        body: JSON.stringify(updates),
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  /**
   * One-time migration: send frontend localStorage thread data to backend.
   */
  async migrateThreadMetadata(
    threads: Array<{ id: string; title: string; pinned?: boolean; platform?: string; createdAt: Date; updatedAt: Date }>
  ): Promise<{ migrated_threads: number }> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/metadata/migrate`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ threads }),
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }
  async deleteThread(threadId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}`,
      {
        method: 'DELETE',
        headers: this.getHeaders(),
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }
  async getThreadHistory(threadId: string): Promise<ThreadHistory> {
    // History filtering for autonomous wake-ups is driven by the global
    // localStorage toggle (default ON). A per-thread `show_autonomous_prompts`
    // value of true acts as a force-on override (e.g. when the global is off
    // but a specific thread should still show them). The backend honors the
    // query param when present; otherwise it falls back to the per-thread
    // field (preserves behavior for MCP/CLI and older clients).
    const perThreadCfg = threadConfigStore.getConfig(threadId);
    const effectiveShowAutonomousPrompts =
      configStore.showAutonomousPrompts || Boolean(perThreadCfg?.showAutonomousPrompts);
    const params = new URLSearchParams({
      show_autonomous_prompts: String(effectiveShowAutonomousPrompts),
    });
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${threadId}/history?${params.toString()}`,
      {
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API messages to our format
    const messages: Message[] = (data.messages || []).map((m: Record<string, unknown>) => {
      const steps = Array.isArray(m.steps)
        ? m.steps
            .map((step) => this.normalizeMessageStep(step))
            .filter((step): step is MessageStep => step !== null)
        : undefined;
      const hasSteps = !!steps?.length;
      const legacyToolCalls = !hasSteps && Array.isArray(m.tool_calls)
        ? m.tool_calls
            .map((toolCall) => this.normalizeToolCall(toolCall))
            .filter((toolCall): toolCall is ToolCall => toolCall !== null)
        : undefined;

      return {
        id: (m.id as string) || crypto.randomUUID(),
        role: m.role as 'user' | 'assistant' | 'system',
        kind: m.kind as Message['kind'],
        content: m.content as string,
        steps,
        timestamp: new Date((m.timestamp as string) || Date.now()),
        status: 'complete' as const,
        ...(!hasSteps ? {
          intermediateContent: m.intermediate_content as string | undefined,
          toolCalls: legacyToolCalls,
        } : {}),
        attachments: m.attachments as Message['attachments'],
        contextSummary: (m.context_summary as string | undefined) || (m.contextSummary as string | undefined),
        messagesRemoved: (m.messages_removed as number | undefined) ?? (m.messagesRemoved as number | undefined),
        autoResumed: (m.auto_resumed as boolean | undefined) ?? (m.autoResumed as boolean | undefined),
        autonomousSource: m.autonomous_source as string | undefined,
        toolReloadInfo: m.tool_reload_info ? {
          tools: ((m.tool_reload_info as Record<string, unknown>).tools as string[]) || [],
          ttl: ((m.tool_reload_info as Record<string, unknown>).ttl as string) || '',
          ttlSeconds: ((m.tool_reload_info as Record<string, unknown>).ttl_seconds as number | null) ?? null,
          source: ((m.tool_reload_info as Record<string, unknown>).source as string) || undefined,
          skillName: ((m.tool_reload_info as Record<string, unknown>).skill_name as string | null) || undefined,
          reason: ((m.tool_reload_info as Record<string, unknown>).reason as string | null) || undefined,
          resumePrompt: ((m.tool_reload_info as Record<string, unknown>).resume_prompt as string) || undefined,
        } : undefined
      };
    });

    return {
      threadId,
      messages
    };
  }
  async getThreadStatus(threadId: string): Promise<ThreadStatus> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/status`,
      {
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      threadId: data.thread_id as string,
      revision: (data.revision as string | null | undefined) ?? null,
      processing: Boolean(data.processing),
    };
  }
  /**
   * Look up per-model attachment caps for this thread. Used to drive the
   * InputBar's "X / Y images" counter and to short-circuit oversize uploads
   * before the user hits Send. Source of truth is the backend's
   * `get_attachment_limits(effective_model)`.
   */
  async getAttachmentLimits(threadId: string): Promise<AttachmentLimitsResponse> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/attachment_limits`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return response.json();
  }

  async stopThread(threadId: string): Promise<void> {
    try {
      await fetch(`${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/stop`, {
        method: 'POST',
        headers: this.getHeaders()
      });
    } catch {
      // Fire-and-forget — the AbortController already dropped the connection
      // and the backend safety net (disconnect detection) will clean up.
    }
  }
  async getThreadContextStats(threadId: string): Promise<ContextStats | null> {
    try {
      const response = await fetch(
        `${this.getBaseUrl()}/threads/${threadId}/context`,
        { headers: this.getHeaders() }
      );

      if (!response.ok) return null;

      const data = await response.json();
      return {
        threadId: data.thread_id as string,
        model: data.model as string,
        totalTokens: data.total_tokens as number,
        inputTokens: data.input_tokens as number,
        outputTokens: data.output_tokens as number,
        contextLimit: data.context_limit as number,
        usagePercentage: data.usage_percentage as number,
        compactionCount: data.compaction_count as number,
        lastCompaction: data.last_compaction as string | null,
        contextManagement: data.context_management as string,
      };
    } catch {
      return null;
    }
  }
}
