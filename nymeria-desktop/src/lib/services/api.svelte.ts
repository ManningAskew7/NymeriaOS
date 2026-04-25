import { configStore } from '$lib/stores/config.svelte';
import { clientId } from '$lib/stores/clientId.svelte';
import type {
  AccountIdentity,
  SSEEvent,
  SSEEventType,
  ChatResponse,
  ThreadHistory,
  Tool,
  Message,
  MessageStep,
  ToolCall,
  ContextStats,
  ServerSettings,
  ServerSettingsUpdate,
  TodoItem,
  TodoListResponse,
  TodoCreateRequest,
  TodoUpdateRequest,
  ScheduledTask,
  ScheduledTasksResponse,
  ActivityEntry,
  ActivityLogResponse,
  Notification,
  NotificationsResponse,
  FileAttachment,
  WorkspaceArtifact,
  AttachmentValidationResult,
  CustomTool,
  HTTPToolConfig,
  CustomToolListResponse,
  CustomToolCreateRequest,
  CustomToolUpdateRequest,
  CustomToolTestRequest,
  CustomToolTestResponse,
  BuiltInTool,
  BuiltInToolsResponse,
  ToolPreferences,
  ToolCategoriesResponse,
  UnifiedTool,
  UnifiedToolListResponse,
  Trigger,
  TriggerCreateRequest,
  TriggerUpdateRequest,
  TriggerSourceInfo,
  TriggerCreatedBy,
  TriggerExecution,
  TriggerTestResult,
  ModelMetadata,
  AvailableModel,
  MCPServer,
  MCPDiscoveredTool,
  MCPServerCreateRequest,
  MCPServerUpdateRequest,
  MCPInstallRequest,
  MCPInstallResponse,
  MCPServerListResponse
} from '$lib/types';

// Module-level abort controller for current stream
let currentAbortController: AbortController | null = null;
// Track which thread the active interactive stream belongs to
let currentStreamThreadId: string | null = null;

/**
 * Abort the current streaming request.
 * Safe to call even if no stream is active.
 */
export function abortCurrentStream(): void {
  if (currentAbortController) {
    currentAbortController.abort();
    currentAbortController = null;
  }
  currentStreamThreadId = null;
}

/** Check if there's an active interactive chat stream for the given thread. */
export function hasActiveStreamForThread(threadId: string): boolean {
  return currentAbortController !== null && currentStreamThreadId === threadId;
}

export class NymeriaAPI {
  private getHeaders(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${configStore.apiKey}`,
      'X-Nymeria-Client-Id': clientId
    };
  }

  private getBaseUrl(): string {
    return configStore.apiUrl.replace(/\/$/, '');
  }

  private normalizeWorkspaceArtifact(raw: unknown): WorkspaceArtifact | null {
    if (!raw || typeof raw !== 'object') return null;

    const data = raw as Record<string, unknown>;
    const path = data.path as string | undefined;
    const name = data.name as string | undefined;
    if (!path || !name) return null;

    return {
      path,
      name,
      mimeType: (data.mimeType as string) || (data.mime_type as string) || 'application/octet-stream',
      sizeBytes: Number((data.sizeBytes as number | string | undefined) ?? data.size_bytes ?? 0) || 0
    };
  }

  private normalizeMessageStep(raw: unknown): MessageStep | null {
    if (!raw || typeof raw !== 'object') return null;

    const step = raw as Record<string, unknown>;
    const type = step.type as MessageStep['type'] | undefined;
    if (!type) return null;

    const artifacts = Array.isArray(step.artifacts)
      ? step.artifacts
          .map((artifact) => this.normalizeWorkspaceArtifact(artifact))
          .filter((artifact): artifact is WorkspaceArtifact => artifact !== null)
      : undefined;

    return {
      type,
      content: step.content as string | undefined,
      id: step.id as string | undefined,
      name: step.name as string | undefined,
      arguments: step.arguments as Record<string, unknown> | undefined,
      result: step.result as string | undefined,
      artifacts,
      status: step.status as MessageStep['status'],
      startTime: step.startTime ? new Date(step.startTime as string) : undefined,
      endTime: step.endTime ? new Date(step.endTime as string) : undefined
    };
  }

  private normalizeToolCall(raw: unknown): ToolCall | null {
    if (!raw || typeof raw !== 'object') return null;

    const toolCall = raw as Record<string, unknown>;
    const artifacts = Array.isArray(toolCall.artifacts)
      ? toolCall.artifacts
          .map((artifact) => this.normalizeWorkspaceArtifact(artifact))
          .filter((artifact): artifact is WorkspaceArtifact => artifact !== null)
      : undefined;

    return {
      id: (toolCall.id as string) || crypto.randomUUID(),
      name: (toolCall.name as string) || 'unknown',
      arguments: (toolCall.arguments as Record<string, unknown>) || {},
      result: toolCall.result as string | undefined,
      artifacts,
      status: (toolCall.status as ToolCall['status']) || 'success',
      startTime: toolCall.startTime ? new Date(toolCall.startTime as string) : undefined,
      endTime: toolCall.endTime ? new Date(toolCall.endTime as string) : undefined
    };
  }

  async healthCheck(): Promise<boolean> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/health`, {
        headers: this.getHeaders()
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  async verifyAuth(): Promise<Response> {
    return fetch(`${this.getBaseUrl()}/me`, {
      headers: this.getHeaders()
    });
  }

  async updateMe(displayName: string): Promise<AccountIdentity> {
    const response = await fetch(`${this.getBaseUrl()}/me`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify({ display_name: displayName })
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `Failed to update profile (${response.status})`);
    }
    return response.json();
  }

  async restartServer(): Promise<boolean> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/restart`, {
        method: 'POST',
        headers: this.getHeaders()
      });
      return response.ok;
    } catch {
      // Connection may drop before response — that's expected during restart
      return true;
    }
  }

  async *chatStream(
    message: string,
    threadId?: string,
    attachments?: FileAttachment[],
    forceUnsupportedAttachments: boolean = false
  ): AsyncGenerator<SSEEvent> {
    const url = `${this.getBaseUrl()}/chat`;

    // Create abort controller for this stream
    currentAbortController = new AbortController();
    currentStreamThreadId = threadId || null;

    // Build request body with optional attachments
    const requestBody: Record<string, unknown> = {
      message,
      thread_id: threadId,
      stream: true
    };

    // Add attachments if provided (convert to backend format)
    if (attachments && attachments.length > 0) {
      requestBody.attachments = attachments.map((att) => ({
        file_type: att.type,
        data_url: att.dataUrl,
        mime_type: att.mimeType,
        file_name: att.name
      }));
    }

    if (forceUnsupportedAttachments) {
      requestBody.force_unsupported_attachments = true;
    }

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...this.getHeaders(),
        Accept: 'text/event-stream'
      },
      body: JSON.stringify(requestBody),
      signal: currentAbortController.signal
    });

    if (!response.ok) {
      const errorText = await response.text();
      yield {
        type: 'error',
        data: {
          message: `API error: ${response.status} - ${errorText}`,
          code: response.status.toString()
        },
        timestamp: new Date()
      };
      return;
    }

    const reader = response.body?.getReader();
    if (!reader) {
      yield {
        type: 'error',
        data: { message: 'No response body', code: 'NO_BODY' },
        timestamp: new Date()
      };
      return;
    }

    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();

        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const jsonStr = line.slice(6).trim();
            if (jsonStr === '[DONE]') {
              return;
            }

            try {
              const parsed = JSON.parse(jsonStr);
              const event = this.parseSSEEvent(parsed);
              if (event) {
                yield event;
              }
            } catch (e) {
              console.error('Failed to parse SSE event:', e, jsonStr);
            }
          }
        }
      }

      // Process any remaining buffer
      if (buffer.startsWith('data: ')) {
        const jsonStr = buffer.slice(6).trim();
        if (jsonStr && jsonStr !== '[DONE]') {
          try {
            const parsed = JSON.parse(jsonStr);
            const event = this.parseSSEEvent(parsed);
            if (event) {
              yield event;
            }
          } catch (e) {
            console.error('Failed to parse final SSE event:', e);
          }
        }
      }
    } finally {
      reader.releaseLock();
      currentAbortController = null;
      currentStreamThreadId = null;
    }
  }

  async validateThreadAttachments(
    threadId: string,
    attachments: FileAttachment[]
  ): Promise<AttachmentValidationResult> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/attachments/validate`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          attachments: attachments.map((att) => ({
            file_type: att.type,
            data_url: att.dataUrl,
            mime_type: att.mimeType,
            file_name: att.name
          }))
        })
      }
    );

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Failed to validate attachments: ${response.status} ${text}`);
    }

    return await response.json() as AttachmentValidationResult;
  }

  async downloadWorkspaceFile(path: string): Promise<{
    blob: Blob;
    filename: string;
    contentType: string;
  }> {
    const url = new URL(`${this.getBaseUrl()}/workspace/download`);
    url.searchParams.set('path', path);

    const response = await fetch(url.toString(), {
      headers: {
        Authorization: `Bearer ${configStore.apiKey}`
      }
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Failed to download workspace file: ${response.status} ${text}`);
    }

    const blob = await response.blob();
    const contentType = response.headers.get('content-type') || blob.type || 'application/octet-stream';
    const disposition = response.headers.get('content-disposition') || '';
    const filenameMatch = disposition.match(/filename=\"?([^\";]+)\"?/i);
    const filename = filenameMatch?.[1] || path.split('/').pop() || 'download';

    return { blob, filename, contentType };
  }

  private parseSSEEvent(data: Record<string, unknown>): SSEEvent | null {
    const eventType = data.type as SSEEventType;
    // Extract thread_id from every event - backend sends it with all events
    const threadId = data.thread_id as string | undefined;

    // Handle events with explicit type field (Nymeria API format)
    if (eventType) {
      switch (eventType) {
        case 'thinking':
          return {
            type: 'thinking',
            data: { message: (data.content as string) || '' },
            timestamp: new Date(),
            threadId
          };

        case 'response':
          return {
            type: 'response',
            data: {
              content: (data.content as string) || '',
              isComplete: false
            },
            timestamp: new Date(),
            threadId
          };

        case 'tool_call':
          // Nymeria sends: { type, id, name, args }
          return {
            type: 'tool_call',
            data: {
              id: (data.id as string) || `${data.name}-${Date.now()}`,
              name: data.name as string,
              arguments: (data.args as Record<string, unknown>) || {}
            },
            timestamp: new Date(),
            threadId
          };

        case 'tool_result':
          // Nymeria sends: { type, id, name, result }
          return {
            type: 'tool_result',
            data: {
              id: data.id as string | undefined,
              name: data.name as string,
              result: (data.result as string) || '',
              status: 'success' as const
            },
            timestamp: new Date(),
            threadId
          };

        case 'workspace_artifact': {
          const artifact = this.normalizeWorkspaceArtifact(data);
          if (!artifact) return null;
          return {
            type: 'workspace_artifact',
            data: {
              toolCallId: data.tool_call_id as string | undefined,
              toolName: (data.tool_name as string) || '',
              artifact
            },
            timestamp: new Date(),
            threadId
          };
        }

        case 'error':
          return {
            type: 'error',
            data: {
              message: (data.content as string) || (data.error as string) || 'Unknown error',
              code: data.code as string | undefined,
              details: data.details as Record<string, unknown> | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'done': {
          // Map snake_case context_stats to camelCase ContextStats
          const rawStats = data.context_stats as Record<string, unknown> | undefined;
          const contextStats = rawStats ? {
            threadId: rawStats.thread_id as string,
            model: (rawStats.model as string) || '',
            totalTokens: rawStats.total_tokens as number,
            inputTokens: rawStats.input_tokens as number,
            outputTokens: rawStats.output_tokens as number,
            contextLimit: rawStats.context_limit as number,
            usagePercentage: rawStats.usage_percentage as number,
            compactionCount: rawStats.compaction_count as number,
            lastCompaction: rawStats.last_compaction as string | null,
            contextManagement: rawStats.context_management as string,
          } : undefined;
          return {
            type: 'done',
            data: {
              threadId: threadId || '',
              contextStats,
              model: data.model as string | undefined,
            },
            timestamp: new Date(),
            threadId
          };
        }

        case 'queued':
          return {
            type: 'queued',
            data: {
              message: (data.content as string) || 'Waiting for autonomous task to finish...',
              holder: (data.holder as string) || undefined,
              heldSeconds: (data.held_seconds as number) || undefined
            },
            timestamp: new Date(),
            threadId
          };

        case 'compacting':
          return {
            type: 'compacting',
            data: { message: (data.message as string) || 'Compacting conversation...' },
            timestamp: new Date(),
            threadId
          };

        case 'compact_result':
          return {
            type: 'compact_result',
            data: {
              success: (data.result as { success?: boolean })?.success ?? false,
              messagesRemoved: (data.result as { messages_removed?: number })?.messages_removed ?? 0,
              reason: (data.result as { reason?: string })?.reason
            },
            timestamp: new Date(),
            threadId
          };

        case 'compacted':
          return {
            type: 'compacted',
            data: {
              messagesRemoved: (data.messages_removed as number) || 0,
              autoResumed: (data.auto_resumed as boolean) || false,
              summary: (data.summary as string) || undefined
            },
            timestamp: new Date(),
            threadId
          };

        case 'context_attached':
          return {
            type: 'context_attached',
            data: { summary: (data.summary as string) || '' },
            timestamp: new Date(),
            threadId
          };

        case 'iteration_limit':
          return {
            type: 'iteration_limit',
            data: {
              message: (data.content as string) || 'Agent reached the maximum number of steps.',
              maxIterations: (data.max_iterations as number) || 70,
              scope: (data.scope as 'main_agent' | 'sub_agent' | undefined),
              agentName: data.agent_name as string | undefined,
              toolCallCount: data.tool_call_count as number | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'tool_reload':
          return {
            type: 'tool_reload',
            data: {
              tools: (data.tools as string[]) || [],
              ttl: (data.ttl as string) || '',
              ttlSeconds: (data.ttl_seconds as number | null) ?? null,
            },
            timestamp: new Date(),
            threadId
          };
      }
    }

    // Fallback: try to infer type from data structure
    if (data.thinking) {
      return {
        type: 'thinking',
        data: { message: data.thinking as string },
        timestamp: new Date(),
        threadId
      };
    }
    if (data.content !== undefined && !eventType) {
      return {
        type: 'response',
        data: {
          content: data.content as string,
          isComplete: data.is_complete === true
        },
        timestamp: new Date(),
        threadId
      };
    }
    if (data.error) {
      return {
        type: 'error',
        data: {
          message: data.error as string,
          code: data.code as string | undefined
        },
        timestamp: new Date(),
        threadId
      };
    }
    if (data.done || (data.thread_id && !eventType)) {
      return {
        type: 'done',
        data: {
          threadId: threadId || '',
        },
        timestamp: new Date(),
        threadId
      };
    }

    return null;
  }

  async chatSync(message: string, threadId?: string): Promise<ChatResponse> {
    const response = await fetch(`${this.getBaseUrl()}/chat`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        message,
        thread_id: threadId,
        stream: false
      })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

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
      platform_meta: Record<string, string> | null;
      created_at: string | null;
      updated_at: string | null;
      title_source: string;
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
    await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}`,
      {
        method: 'DELETE',
        headers: this.getHeaders(),
      }
    );
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
        autonomousSource: m.autonomous_source as string | undefined,
        toolReloadInfo: m.tool_reload_info ? {
          tools: ((m.tool_reload_info as Record<string, unknown>).tools as string[]) || [],
          ttl: ((m.tool_reload_info as Record<string, unknown>).ttl as string) || '',
          resumePrompt: ((m.tool_reload_info as Record<string, unknown>).resume_prompt as string) || undefined,
        } : undefined
      })
    );

    return {
      threadId,
      messages
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
      };
    } catch {
      return null;
    }
  }

  async getTools(): Promise<Tool[]> {
    const response = await fetch(`${this.getBaseUrl()}/tools`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  async getServerSettings(): Promise<ServerSettings> {
    const response = await fetch(`${this.getBaseUrl()}/settings`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  async updateServerSettings(
    updates: ServerSettingsUpdate
  ): Promise<{ message: string; updated: string[]; restart_required: boolean }> {
    const response = await fetch(`${this.getBaseUrl()}/settings`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify(updates)
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  // Model Metadata

  async getOpenRouterModels(): Promise<ModelMetadata[]> {
    try {
      const response = await fetch(`${this.getBaseUrl()}/models`, {
        headers: this.getHeaders()
      });

      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }

  async getAvailableModels(provider?: string): Promise<AvailableModel[]> {
    try {
      const params = provider ? `?provider=${encodeURIComponent(provider)}` : '';
      const response = await fetch(`${this.getBaseUrl()}/models/available${params}`, {
        headers: this.getHeaders()
      });

      if (!response.ok) return [];
      return response.json();
    } catch {
      return [];
    }
  }

  // Dashboard API Methods

  async getThreadTaskCounts(): Promise<Record<string, number>> {
    const response = await fetch(`${this.getBaseUrl()}/todos/thread-counts`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
    return response.json();
  }

  async getTodos(filterStatus?: string, threadId?: string): Promise<TodoListResponse> {
    const params = new URLSearchParams();
    if (filterStatus) {
      params.set('filter_status', filterStatus);
    }
    if (threadId) {
      params.set('thread_id', threadId);
    }

    const url = `${this.getBaseUrl()}/todos${params.toString() ? `?${params}` : ''}`;
    const response = await fetch(url, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      userId: data.user_id,
      items: (data.items || []).map(
        (item: Record<string, unknown>) =>
          ({
            id: item.id as string,
            task: item.task as string,
            status: item.status as string,
            createdAt: this.parseUtcTimestamp(item.created_at as string),
            updatedAt: this.parseUtcTimestamp(item.updated_at as string),
            notes: item.notes as string | undefined,
            // Scheduling fields
            scheduledFor: item.scheduled_for ? this.parseUtcTimestamp(item.scheduled_for as string) : undefined,
            threadId: item.thread_id as string | undefined,
            lastExecution: item.last_execution ? this.parseUtcTimestamp(item.last_execution as string) : undefined,
            // User management & recurrence fields
            createdBy: (item.created_by as string) || 'agent',
            recurrence: item.recurrence as string | undefined
          }) as TodoItem
      ),
      total: data.total
    };
  }

  private todoFromResponse(item: Record<string, unknown>): TodoItem {
    return {
      id: item.id as string,
      task: item.task as string,
      status: item.status as string,
      createdAt: this.parseUtcTimestamp(item.created_at as string),
      updatedAt: this.parseUtcTimestamp(item.updated_at as string),
      notes: item.notes as string | undefined,
      scheduledFor: item.scheduled_for ? this.parseUtcTimestamp(item.scheduled_for as string) : undefined,
      threadId: item.thread_id as string | undefined,
      lastExecution: item.last_execution ? this.parseUtcTimestamp(item.last_execution as string) : undefined,
      createdBy: (item.created_by as string) || 'agent',
      recurrence: item.recurrence as string | undefined
    } as TodoItem;
  }

  async createTodo(request: TodoCreateRequest): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        task: request.task,
        notes: request.notes,
        scheduled_for: request.scheduledFor,
        recurrence: request.recurrence,
        thread_id: request.threadId
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async updateTodo(todoId: string, request: TodoUpdateRequest): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify({
        task: request.task,
        status: request.status,
        notes: request.notes,
        scheduled_for: request.scheduledFor,
        recurrence: request.recurrence,
        thread_id: request.threadId,
        clear_schedule: request.clearSchedule,
        clear_recurrence: request.clearRecurrence
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async deleteTodo(todoId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }
  }

  async completeTodo(todoId: string): Promise<TodoItem> {
    const response = await fetch(`${this.getBaseUrl()}/todos/${todoId}/complete`, {
      method: 'POST',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.todoFromResponse(data);
  }

  async getScheduledTasks(): Promise<ScheduledTasksResponse> {
    const response = await fetch(`${this.getBaseUrl()}/tasks`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      tasks: (data.tasks || []).map(
        (task: Record<string, unknown>) =>
          ({
            id: task.id as string,
            prompt: task.prompt as string,
            executeAt: this.parseUtcTimestamp(task.execute_at as string),
            status: task.status as string,
            createdAt: this.parseUtcTimestamp(task.created_at as string),
            threadId: task.thread_id as string
          }) as ScheduledTask
      ),
      total: data.total
    };
  }

  async getActivity(limit: number = 50, threadId?: string): Promise<ActivityLogResponse> {
    const params = new URLSearchParams();
    params.set('limit', limit.toString());
    if (threadId) {
      params.set('thread_id', threadId);
    }

    const response = await fetch(`${this.getBaseUrl()}/activity?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      entries: (data.entries || []).map(
        (entry: Record<string, unknown>) =>
          ({
            id: entry.id as string,
            timestamp: this.parseUtcTimestamp(entry.timestamp as string),
            type: entry.type as string,
            message: entry.message as string,
            threadId: entry.thread_id as string | undefined,
            metadata: entry.metadata as Record<string, unknown> | undefined
          }) as ActivityEntry
      ),
      total: data.total
    };
  }

  // Notification API Methods

  async getNotifications(): Promise<NotificationsResponse> {
    const response = await fetch(`${this.getBaseUrl()}/notifications`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert API response to our format
    return {
      notifications: (data.notifications || []).map(
        (n: Record<string, unknown>) =>
          ({
            id: n.id as string,
            summary: n.summary as string,
            threadId: n.thread_id as string | undefined,
            taskId: n.task_id as string | undefined,
            createdAt: this.parseUtcTimestamp(n.created_at as string),
            read: n.read as boolean
          }) as Notification
      ),
      unreadCount: data.unread_count as number
    };
  }

  async markNotificationRead(notificationId: string): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/${notificationId}/read`,
      {
        method: 'POST',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async markAllNotificationsRead(): Promise<void> {
    const response = await fetch(
      `${this.getBaseUrl()}/notifications/read-all`,
      {
        method: 'POST',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  // Parse timestamp as UTC (backend sends timestamps without timezone suffix)
  private parseUtcTimestamp(timestamp: string): Date {
    // If timestamp doesn't have timezone info, treat it as UTC
    if (!timestamp.endsWith('Z') && !timestamp.includes('+') && !timestamp.includes('-', 10)) {
      return new Date(timestamp + 'Z');
    }
    return new Date(timestamp);
  }

  // =========================================================================
  // Custom Tools API
  // =========================================================================

  private customToolFromResponse(item: Record<string, unknown>): CustomTool {
    return {
      id: item.id as string,
      name: item.name as string,
      description: item.description as string,
      parameters: item.parameters as Record<string, unknown> as CustomTool['parameters'],
      implementationType: item.implementation_type as CustomTool['implementationType'],
      httpConfig: item.http_config
        ? {
            method: (item.http_config as Record<string, unknown>).method as HTTPToolConfig['method'],
            url: (item.http_config as Record<string, unknown>).url as string,
            headers: ((item.http_config as Record<string, unknown>).headers || {}) as Record<string, string>,
            bodyTemplate: (item.http_config as Record<string, unknown>).body_template as string | undefined,
            queryParams: ((item.http_config as Record<string, unknown>).query_params || {}) as Record<string, string>,
            timeoutSeconds: ((item.http_config as Record<string, unknown>).timeout_seconds || 30) as number,
            responsePath: (item.http_config as Record<string, unknown>).response_path as string | undefined,
            responseFormat: ((item.http_config as Record<string, unknown>).response_format || 'auto') as HTTPToolConfig['responseFormat']
          }
        : undefined,
      mcpConfig: item.mcp_config
        ? {
            serverCommand: (item.mcp_config as Record<string, unknown>).server_command as string,
            serverArgs: ((item.mcp_config as Record<string, unknown>).server_args || []) as string[],
            toolName: (item.mcp_config as Record<string, unknown>).tool_name as string,
            envVars: ((item.mcp_config as Record<string, unknown>).env_vars || {}) as Record<string, string>,
            workingDirectory: (item.mcp_config as Record<string, unknown>).working_directory as string | undefined,
            idleTimeoutSeconds: ((item.mcp_config as Record<string, unknown>).idle_timeout_seconds || 300) as number,
            startupTimeoutSeconds: ((item.mcp_config as Record<string, unknown>).startup_timeout_seconds || 30) as number
          }
        : undefined,
      enabled: (item.enabled ?? true) as boolean,
      tags: (item.tags || []) as string[],
      createdAt: this.parseUtcTimestamp(item.created_at as string),
      updatedAt: this.parseUtcTimestamp(item.updated_at as string)
    };
  }

  async getCustomTools(): Promise<CustomToolListResponse> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.customToolFromResponse(item)
      ),
      total: data.total
    };
  }

  async createCustomTool(request: CustomToolCreateRequest): Promise<CustomTool> {
    const body: Record<string, unknown> = {
      id: request.id,
      name: request.name,
      description: request.description,
      parameters: request.parameters || {},
      implementation_type: request.implementationType,
      enabled: request.enabled ?? true,
      tags: request.tags || []
    };

    if (request.httpConfig) {
      body.http_config = {
        method: request.httpConfig.method,
        url: request.httpConfig.url,
        headers: request.httpConfig.headers || {},
        body_template: request.httpConfig.body_template,
        query_params: request.httpConfig.query_params || {},
        timeout_seconds: request.httpConfig.timeout_seconds || 30,
        response_path: request.httpConfig.response_path,
        response_format: request.httpConfig.response_format || 'auto'
      };
    }

    if (request.mcpConfig) {
      body.mcp_config = {
        server_command: request.mcpConfig.server_command,
        server_args: request.mcpConfig.server_args || [],
        tool_name: request.mcpConfig.tool_name,
        env_vars: request.mcpConfig.env_vars || {},
        working_directory: request.mcpConfig.working_directory,
        idle_timeout_seconds: request.mcpConfig.idle_timeout_seconds || 300,
        startup_timeout_seconds: request.mcpConfig.startup_timeout_seconds || 30
      };
    }

    const response = await fetch(`${this.getBaseUrl()}/tools/custom`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async getCustomTool(toolId: string): Promise<CustomTool> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async updateCustomTool(toolId: string, request: CustomToolUpdateRequest): Promise<CustomTool> {
    const body: Record<string, unknown> = {};

    if (request.name !== undefined) body.name = request.name;
    if (request.description !== undefined) body.description = request.description;
    if (request.parameters !== undefined) body.parameters = request.parameters;
    if (request.enabled !== undefined) body.enabled = request.enabled;
    if (request.tags !== undefined) body.tags = request.tags;

    if (request.httpConfig) {
      body.http_config = {
        method: request.httpConfig.method,
        url: request.httpConfig.url,
        headers: request.httpConfig.headers || {},
        body_template: request.httpConfig.body_template,
        query_params: request.httpConfig.query_params || {},
        timeout_seconds: request.httpConfig.timeout_seconds || 30,
        response_path: request.httpConfig.response_path,
        response_format: request.httpConfig.response_format || 'auto'
      };
    }

    if (request.mcpConfig) {
      body.mcp_config = {
        server_command: request.mcpConfig.server_command,
        server_args: request.mcpConfig.server_args || [],
        tool_name: request.mcpConfig.tool_name,
        env_vars: request.mcpConfig.env_vars || {},
        working_directory: request.mcpConfig.working_directory,
        idle_timeout_seconds: request.mcpConfig.idle_timeout_seconds || 300,
        startup_timeout_seconds: request.mcpConfig.startup_timeout_seconds || 30
      };
    }

    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(body)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.customToolFromResponse(data);
  }

  async deleteCustomTool(toolId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }
  }

  async testCustomTool(toolId: string, params: Record<string, unknown>): Promise<CustomToolTestResponse> {
    const startTime = performance.now();
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/${toolId}/test`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ params })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    const executionTimeMs = Math.round(performance.now() - startTime);
    return {
      status: data.status,
      toolId: data.tool_id,
      result: data.result,
      error: data.error,
      success: data.status === 'ok',
      executionTimeMs
    };
  }

  async exportCustomTools(): Promise<{ tools: CustomTool[]; total: number; exportedAt: string }> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/export`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.customToolFromResponse(item)
      ),
      total: data.total,
      exportedAt: data.exported_at
    };
  }

  async importCustomTools(
    tools: CustomToolCreateRequest[]
  ): Promise<{ imported: number; errors: string[] }> {
    const response = await fetch(`${this.getBaseUrl()}/tools/custom/import`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ tools })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  // =========================================================================
  // MCP Servers API
  // =========================================================================

  private mcpServerFromResponse(item: Record<string, unknown>): MCPServer {
    return {
      id: item.id as string,
      name: item.name as string,
      description: (item.description as string) || '',
      serverCommand: item.server_command as string,
      serverArgs: (item.server_args as string[]) || [],
      envVars: (item.env_vars as Record<string, string>) || {},
      workingDirectory: item.working_directory as string | undefined,
      idleTimeoutSeconds: (item.idle_timeout_seconds as number) || 300,
      startupTimeoutSeconds: (item.startup_timeout_seconds as number) || 30,
      enabled: item.enabled as boolean,
      discoveredTools: ((item.discovered_tools as Array<Record<string, unknown>>) || []).map(t => ({
        name: t.name as string,
        description: (t.description as string) || '',
        inputSchema: (t.input_schema as Record<string, unknown>) || {},
      })),
      createdAt: item.created_at as string,
      updatedAt: item.updated_at as string,
    };
  }

  async listMCPServers(): Promise<MCPServerListResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      servers: (data.servers || []).map((s: Record<string, unknown>) => this.mcpServerFromResponse(s)),
      total: data.total,
    };
  }

  async createMCPServer(request: MCPServerCreateRequest, threadId?: string): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
    const url = threadId
      ? `${this.getBaseUrl()}/mcp-servers?thread_id=${encodeURIComponent(threadId)}`
      : `${this.getBaseUrl()}/mcp-servers`;
    const response = await fetch(url, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `API error: ${response.status}`);
    }
    const data = await response.json();
    return {
      server: this.mcpServerFromResponse(data.server),
      discoveredTools: data.discovered_tools,
      discoveryError: data.discovery_error,
    };
  }

  async installMCPServer(request: MCPInstallRequest): Promise<MCPInstallResponse> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/install`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `API error: ${response.status}`);
    }
    const data = await response.json();
    return {
      server: this.mcpServerFromResponse(data.server),
      parsedSummary: data.parsed_summary || '',
      discoveredTools: data.discovered_tools ?? 0,
      toolNames: data.tool_names || [],
      threadId: data.thread_id ?? undefined,
    };
  }

  async updateMCPServer(serverId: string, request: MCPServerUpdateRequest): Promise<{ server: MCPServer; discoveredTools: number; discoveryError?: string }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      server: this.mcpServerFromResponse(data.server),
      discoveredTools: data.discovered_tools,
      discoveryError: data.discovery_error,
    };
  }

  async deleteMCPServer(serverId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
  }

  async discoverMCPServerTools(serverId: string): Promise<{ discoveredTools: MCPDiscoveredTool[]; count: number }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}/discover`, {
      method: 'POST',
      headers: this.getHeaders()
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `API error: ${response.status}`);
    }
    const data = await response.json();
    return {
      discoveredTools: (data.discovered_tools || []).map((t: Record<string, unknown>) => ({
        name: t.name as string,
        description: (t.description as string) || '',
        inputSchema: (t.input_schema as Record<string, unknown>) || {},
      })),
      count: data.count,
    };
  }

  async testMCPServer(serverId: string): Promise<{ status: string; toolsCount?: number; toolNames?: string[]; error?: string }> {
    const response = await fetch(`${this.getBaseUrl()}/mcp-servers/${serverId}/test`, {
      method: 'POST',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return {
      status: data.status,
      toolsCount: data.tools_count,
      toolNames: data.tool_names,
      error: data.error,
    };
  }

  // =========================================================================
  // Built-in Tools API
  // =========================================================================

  private builtInToolFromResponse(item: Record<string, unknown>): BuiltInTool {
    return {
      name: item.name as string,
      description: item.description as string,
      category: item.category as BuiltInTool['category'],
      securityLevel: item.security_level as BuiltInTool['securityLevel'],
      defaultEnabled: item.default_enabled as boolean,
      enabled: item.enabled as boolean,
      enabledReason: item.enabled_reason as BuiltInTool['enabledReason'],
      globallyDisabled: item.globally_disabled as boolean,
      configSchema: item.config_schema as Record<string, unknown> | undefined,
      userConfig: item.user_config as Record<string, unknown> | undefined
    };
  }

  async getBuiltInTools(userId: string = 'default'): Promise<BuiltInToolsResponse> {
    const response = await fetch(`${this.getBaseUrl()}/users/${userId}/tools`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();

    // Convert tools
    const tools = (data.tools || []).map((item: Record<string, unknown>) =>
      this.builtInToolFromResponse(item)
    );

    // Convert by_category
    const byCategory: Record<string, BuiltInTool[]> = {};
    if (data.by_category) {
      for (const [category, categoryTools] of Object.entries(data.by_category)) {
        byCategory[category] = (categoryTools as Record<string, unknown>[]).map((item) =>
          this.builtInToolFromResponse(item)
        );
      }
    }

    return {
      userId: data.user_id,
      tools,
      byCategory: byCategory as BuiltInToolsResponse['byCategory'],
      total: data.total
    };
  }

  async getToolPreferences(userId: string = 'default'): Promise<ToolPreferences> {
    const response = await fetch(`${this.getBaseUrl()}/users/${userId}/tools/preferences`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      enabledOverrides: data.enabled_overrides || {},
      disabledCategories: data.disabled_categories || [],
      toolConfigs: data.tool_configs || {}
    };
  }

  async setToolEnabled(
    userId: string,
    toolName: string,
    enabled: boolean
  ): Promise<{ status: string; toolName: string; enabled: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/${toolName}/enable`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ enabled })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolName: data.tool_name,
      enabled: data.enabled
    };
  }

  async clearToolOverride(
    userId: string,
    toolName: string
  ): Promise<{ status: string; toolName: string; cleared: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/${toolName}/enable`,
      {
        method: 'DELETE',
        headers: this.getHeaders()
      }
    );

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolName: data.tool_name,
      cleared: data.cleared
    };
  }

  async setCategoryEnabled(
    userId: string,
    category: string,
    enabled: boolean
  ): Promise<{ status: string; category: string; enabled: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/categories/${category}/enable`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ enabled })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      category: data.category,
      enabled: data.enabled
    };
  }

  async setToolConfig(
    userId: string,
    toolName: string,
    config: Record<string, unknown>
  ): Promise<{ status: string; toolName: string; config: Record<string, unknown> }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/${toolName}/config`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ config })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolName: data.tool_name,
      config: data.config
    };
  }

  async resetToolPreferences(userId: string): Promise<{ status: string; message: string }> {
    const response = await fetch(`${this.getBaseUrl()}/users/${userId}/tools/reset`, {
      method: 'POST',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return response.json();
  }

  async getToolCategories(): Promise<ToolCategoriesResponse> {
    const response = await fetch(`${this.getBaseUrl()}/tools/categories`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      categories: data.categories
    };
  }

  // =========================================================================
  // Unified Tools API
  // =========================================================================

  private unifiedToolFromResponse(item: Record<string, unknown>): UnifiedTool {
    return {
      id: item.id as string,
      name: item.name as string,
      description: item.description as string,
      defaultDescription: item.default_description as string,
      customDescription: item.custom_description as string | null | undefined,
      category: item.category as string,
      securityLevel: item.security_level as UnifiedTool['securityLevel'],
      enabled: item.enabled as boolean,
      enabledReason: item.enabled_reason as UnifiedTool['enabledReason'],
      toolType: item.tool_type as UnifiedTool['toolType'],
      implementationType: (item.implementation_type as UnifiedTool['implementationType']) || null,
      configSchema: item.config_schema as Record<string, unknown> | undefined,
      userConfig: (item.user_config as Record<string, unknown>) || {},
      configurable: item.configurable as boolean,
      parameters: item.parameters as Record<string, unknown> | undefined,
      httpConfig: item.http_config as UnifiedTool['httpConfig'],
      mcpConfig: item.mcp_config as UnifiedTool['mcpConfig'],
      tags: (item.tags as string[]) || [],
      editable: item.editable as boolean,
      createdAt: item.created_at as string | undefined,
      updatedAt: item.updated_at as string | undefined
    };
  }

  async getUnifiedTools(userId: string = 'default'): Promise<UnifiedToolListResponse> {
    const response = await fetch(`${this.getBaseUrl()}/users/${userId}/tools/unified`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return {
      tools: (data.tools || []).map((item: Record<string, unknown>) =>
        this.unifiedToolFromResponse(item)
      ),
      total: data.total,
      builtinCount: data.builtin_count,
      customCount: data.custom_count
    };
  }

  async setUnifiedToolEnabled(
    userId: string,
    toolId: string,
    enabled: boolean
  ): Promise<{ status: string; toolId: string; enabled: boolean; toolType: string }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/unified/${toolId}/enable`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ enabled })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      enabled: data.enabled,
      toolType: data.tool_type
    };
  }

  async setUnifiedToolDescription(
    userId: string,
    toolId: string,
    description: string | null
  ): Promise<{ status: string; toolId: string; action: string; description: string | null }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/unified/${toolId}/description`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ description })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      action: data.action,
      description: data.description
    };
  }

  async setUnifiedToolConfig(
    userId: string,
    toolId: string,
    config: Record<string, unknown>
  ): Promise<{ status: string; toolId: string; action: string; config: Record<string, unknown> }> {
    const response = await fetch(
      `${this.getBaseUrl()}/users/${userId}/tools/unified/${toolId}/config`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ config })
      }
    );

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return {
      status: data.status,
      toolId: data.tool_id,
      action: data.action,
      config: data.config
    };
  }

  async createUnifiedTool(request: {
    id: string;
    name: string;
    description: string;
    parameters?: Record<string, unknown>[];
    http?: Record<string, unknown>;
    mcp?: Record<string, unknown>;
    tags?: string[];
  }): Promise<UnifiedTool> {
    const response = await fetch(`${this.getBaseUrl()}/tools/unified`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.unifiedToolFromResponse(data);
  }

  async updateUnifiedTool(
    toolId: string,
    request: {
      name?: string;
      description?: string;
      parameters?: Record<string, unknown>[];
      http?: Record<string, unknown>;
      mcp?: Record<string, unknown>;
      tags?: string[];
    }
  ): Promise<UnifiedTool> {
    const response = await fetch(`${this.getBaseUrl()}/tools/unified/${toolId}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(request)
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();
    return this.unifiedToolFromResponse(data);
  }

  async deleteUnifiedTool(toolId: string): Promise<{ status: string; deleted: string }> {
    const response = await fetch(`${this.getBaseUrl()}/tools/unified/${toolId}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`API error: ${response.status} - ${errorText}`);
    }

    return response.json();
  }

  // =========================================================================
  // Thread Configuration
  // =========================================================================

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
      enabledSkills: data.enabled_skills ?? [],
      disabledSkills: data.disabled_skills ?? [],
      injectTodosInPrompt: data.inject_todos_in_prompt ?? false,
      showAutonomousPrompts: data.show_autonomous_prompts ?? false,
      showPromptMetadata: data.show_prompt_metadata ?? false,
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

  async getOptionalTools(): Promise<import('$lib/types').OptionalTool[]> {
    const response = await fetch(`${this.getBaseUrl()}/tools/optional`, {
      headers: this.getHeaders()
    });
    if (!response.ok) return [];
    const data = await response.json();
    return data.tools ?? [];
  }

  async getDefaultTools(userId: string = 'default'): Promise<import('$lib/types').DefaultToolsResponse> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`Failed to load default tools: ${response.status}`);
    return response.json();
  }

  async setDefaultTools(toolNames: string[], userId: string = 'default'): Promise<void> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify({ tool_names: toolNames })
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Failed to save default tools: ${response.status}`);
    }
  }

  async resetDefaultTools(userId: string = 'default'): Promise<void> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(`${this.getBaseUrl()}/tools/defaults?${params}`, {
      method: 'DELETE',
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`Failed to reset default tools: ${response.status}`);
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
  // Agent Skills API
  // =========================================================================

  async listSkills(
    userId: string = 'default',
    scope?: import('$lib/types').SkillScope,
  ): Promise<import('$lib/types').SkillMetadata[]> {
    const params = new URLSearchParams({ user_id: userId });
    if (scope) params.set('scope', scope);
    const response = await fetch(`${this.getBaseUrl()}/skills?${params.toString()}`, {
      headers: this.getHeaders()
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.skills ?? []) as import('$lib/types').SkillMetadata[];
  }

  async getSkill(
    name: string,
    userId: string = 'default',
  ): Promise<import('$lib/types').SkillDetail> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/${encodeURIComponent(name)}?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return (await response.json()) as import('$lib/types').SkillDetail;
  }

  async installSkill(
    request: import('$lib/types').SkillInstallRequest,
    userId: string = 'default',
  ): Promise<import('$lib/types').SkillMetadata> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/install?${params.toString()}`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify(request),
      },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Install failed: ${response.status} - ${errorText}`);
    }
    const data = await response.json();
    return data.skill as import('$lib/types').SkillMetadata;
  }

  async uninstallSkill(
    name: string,
    scope: 'user' | 'global' = 'user',
    userId: string = 'default',
  ): Promise<void> {
    const params = new URLSearchParams({ scope, user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/skills/${encodeURIComponent(name)}?${params.toString()}`,
      { method: 'DELETE', headers: this.getHeaders() },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Uninstall failed: ${response.status} - ${errorText}`);
    }
  }

  async searchSkillsMarketplace(
    source: import('$lib/types').SkillMarketplaceSource = 'anthropic',
    query?: string,
  ): Promise<import('$lib/types').MarketplaceSkillEntry[]> {
    const params = new URLSearchParams({ source });
    if (query) params.set('q', query);
    const response = await fetch(
      `${this.getBaseUrl()}/skills/marketplace/search?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Marketplace search failed: ${response.status} - ${errorText}`);
    }
    const data = await response.json();
    return (data.results ?? []) as import('$lib/types').MarketplaceSkillEntry[];
  }

  async getThreadActiveSkills(
    threadId: string,
    userId: string = 'default',
  ): Promise<import('$lib/types').ThreadActiveSkillsResponse> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/skills?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return (await response.json()) as import('$lib/types').ThreadActiveSkillsResponse;
  }

  async getGlobalSkills(userId: string = 'default'): Promise<string[]> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/settings/global-skills?${params.toString()}`,
      { headers: this.getHeaders() },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.enabled_global_skills ?? []) as string[];
  }

  async setGlobalSkills(
    skillNames: string[],
    userId: string = 'default',
  ): Promise<string[]> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(
      `${this.getBaseUrl()}/settings/global-skills?${params.toString()}`,
      {
        method: 'PUT',
        headers: this.getHeaders(),
        body: JSON.stringify({ skill_names: skillNames }),
      },
    );
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    return (data.enabled_global_skills ?? []) as string[];
  }

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

  async getTriggers(userId: string = 'default'): Promise<Trigger[]> {
    const params = new URLSearchParams({ user_id: userId });
    const response = await fetch(`${this.getBaseUrl()}/triggers?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return (data || []).map((item: Record<string, unknown>) => this.triggerFromResponse(item));
  }

  async createTrigger(request: TriggerCreateRequest, userId: string = 'default'): Promise<Trigger> {
    const params = new URLSearchParams({ user_id: userId });
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
    userId: string = 'default'
  ): Promise<Trigger> {
    const params = new URLSearchParams({ user_id: userId });
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

  async deleteTrigger(triggerId: string, userId: string = 'default'): Promise<void> {
    const params = new URLSearchParams({ user_id: userId });
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

  async testTrigger(triggerId: string, userId: string = 'default'): Promise<TriggerTestResult> {
    const params = new URLSearchParams({ user_id: userId });
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
    userId: string = 'default',
    limit: number = 50
  ): Promise<TriggerExecution[]> {
    const params = new URLSearchParams({ user_id: userId, limit: limit.toString() });
    const response = await fetch(`${this.getBaseUrl()}/triggers/${triggerId}/executions?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return await response.json();
  }

  async getRecentTriggerExecutions(
    userId: string = 'default',
    limit: number = 50
  ): Promise<TriggerExecution[]> {
    const params = new URLSearchParams({ user_id: userId, limit: limit.toString() });
    const response = await fetch(`${this.getBaseUrl()}/triggers/executions/recent?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    return await response.json();
  }

  // Error Reporting

  async reportProblem(data: {
    thread_id: string | null;
    message_id: string;
    description: string;
    messages: Array<{ role: string; content: string; timestamp: string; id: string }>;
    timestamp: string;
    client_info: Record<string, string>;
  }): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/report`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(data)
    });
    if (!response.ok) {
      throw new Error(`Report failed: ${response.status}`);
    }
  }
}

export const api = new NymeriaAPI();
