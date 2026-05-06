import type {
  ContextStats,
  Message,
  MessageStep,
  ThreadHistory,
  ThreadStatus,
  ToolCall
} from '$lib/types';
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
      throw new Error(await this._toastAndExtractError(response, 'Failed to update settings'));
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

  /**
   * Eagerly claim ownership of a locally-generated thread id. Called from
   * `threadsStore.createThread()` so the backend has a `thread_owners` row
   * before any chat-app binding (Telegram/Discord) routes a message into
   * the thread. Without this, the first non-admin caller to hit /chat for
   * the UUID would TOFU-claim and silently transfer ownership.
   *
   * Idempotent on the backend; safe to call multiple times.
   */
  async claimThread(threadId: string): Promise<{ thread_id: string; owner: string }> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/claim`,
      {
        method: 'POST',
        headers: this.getHeaders(),
      }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to claim thread'));
    }
    return response.json();
  }

  async getThreadHistory(threadId: string): Promise<ThreadHistory> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${threadId}/history`,
      {
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API messages to our format
    const messages: Message[] = (data.messages || []).map(
      (m: Record<string, unknown>) => ({
        id: (m.id as string) || crypto.randomUUID(),
        role: m.role as 'user' | 'assistant' | 'system',
        kind: m.kind as Message['kind'],
        content: m.content as string,
        steps: Array.isArray(m.steps)
          ? m.steps
              .map((step) => this.normalizeMessageStep(step))
              .filter((step): step is MessageStep => step !== null)
          : undefined,
        intermediateContent: m.intermediate_content as string | undefined,
        timestamp: new Date((m.timestamp as string) || Date.now()),
        status: 'complete' as const,
        toolCalls: Array.isArray(m.tool_calls)
          ? m.tool_calls
              .map((toolCall) => this.normalizeToolCall(toolCall))
              .filter((toolCall): toolCall is ToolCall => toolCall !== null)
          : undefined,
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
      })
    );

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
        processing: data.processing as boolean | undefined,
      };
    } catch {
      return null;
    }
  }
}
