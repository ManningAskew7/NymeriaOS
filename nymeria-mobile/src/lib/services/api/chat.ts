import { configStore } from '$lib/stores/config.svelte';
import type {
  AttachmentValidationResult,
  ChatResponse,
  DispatchInfo,
  FileAttachment,
  SSEEvent,
  SSEEventType
} from '$lib/types';
import { CredentialsApi } from './credentials';

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

export class ChatApi extends CredentialsApi {
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
  /**
   * Submit a prompt to a thread that may be mid-turn. Uses its own abort
   * controller so cancelling a queued prompt does not abort the primary stream
   * and vice-versa. Only lifecycle events (prompt_queued / prompt_injected /
   * prompt_absorbed / error) are yielded. Content events arrive on the
   * holder's stream and would otherwise be rendered twice.
   */
  async *queuePromptStream(
    message: string,
    threadId: string,
    abortController: AbortController,
    attachments?: FileAttachment[]
  ): AsyncGenerator<SSEEvent> {
    const url = `${this.getBaseUrl()}/chat`;
    const requestBody: Record<string, unknown> = {
      message,
      thread_id: threadId,
      stream: true
    };

    if (attachments && attachments.length > 0) {
      requestBody.attachments = attachments.map((att) => ({
        file_type: att.type,
        data_url: att.dataUrl,
        mime_type: att.mimeType,
        file_name: att.name
      }));
    }

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        ...this.getHeaders(),
        Accept: 'text/event-stream'
      },
      body: JSON.stringify(requestBody),
      signal: abortController.signal
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
    const LIFECYCLE: ReadonlySet<string> = new Set([
      'prompt_queued',
      'prompt_injected',
      'prompt_absorbed',
      'queued',
      'turn_halted',
      'fanout_dropped',
      'error'
    ]);

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.slice(6).trim();
          if (jsonStr === '[DONE]') return;
          try {
            const parsed = JSON.parse(jsonStr);
            const event = this.parseSSEEvent(parsed);
            if (event && LIFECYCLE.has(event.type)) {
              yield event;
              if (event.type === 'prompt_absorbed' || event.type === 'error') {
                return;
              }
            }
          } catch (e) {
            console.error('Failed to parse queued SSE event:', e, jsonStr);
          }
        }
      }
    } finally {
      reader.releaseLock();
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
    const dispatchedTo = this.normalizeDispatchInfo(data.dispatched_to, data);

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

        case 'dispatched':
          return {
            type: 'dispatched',
            data: dispatchedTo || {
              threadId: (data.target_thread_id as string) || '',
              title: (data.title as string) || 'thread',
              originalThreadId: data.original_thread_id as string | undefined,
              matchedRef: data.matched_ref as string | undefined,
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

        case 'provider_retry':
          return {
            type: 'provider_retry',
            data: {
              provider: data.provider as string | undefined,
              model: data.model as string | undefined,
              providerRoute: data.provider_route as string | null | undefined,
              openaiApiMode: data.openai_api_mode as string | null | undefined,
              attempt: data.attempt as number | undefined,
              maxRetries: data.max_retries as number | undefined,
              delaySeconds: data.delay_seconds as number | undefined,
              reason: data.reason as string | undefined,
              httpStatus: data.http_status as number | null | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'provider_fallback':
          return {
            type: 'provider_fallback',
            data: {
              fromProvider: data.from_provider as string | undefined,
              fromModel: data.from_model as string | undefined,
              toProvider: data.to_provider as string | undefined,
              toModel: data.to_model as string | undefined,
              toProviderRoute: data.to_provider_route as string | null | undefined,
              toOpenaiApiMode: data.to_openai_api_mode as string | null | undefined,
              holdSeconds: data.hold_seconds as number | undefined,
              expiresAt: data.expires_at as string | null | undefined,
              reason: data.reason as string | undefined,
              httpStatus: data.http_status as number | null | undefined,
            },
            timestamp: new Date(),
            threadId
          };

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
              title: data.title as string | undefined,
              title_source: data.title_source as string | undefined,
              dispatchedTo,
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

        case 'prompt_queued':
          return {
            type: 'prompt_queued',
            data: {
              position: (data.position as number) ?? 0,
              holder: (data.holder as string) || undefined,
              heldSeconds: (data.held_seconds as number) || undefined,
              source: (data.source as string) || 'user'
            },
            timestamp: new Date(),
            threadId
          };

        case 'prompt_injected':
          return {
            type: 'prompt_injected',
            data: {
              count: (data.count as number) ?? 1,
              sources: (data.sources as string[]) || []
            },
            timestamp: new Date(),
            threadId
          };

        case 'prompt_absorbed':
          return {
            type: 'prompt_absorbed',
            data: {},
            timestamp: new Date(),
            threadId
          };

        case 'turn_halted':
          return {
            type: 'turn_halted',
            data: {
              reason: (data.reason as string) || 'pending_prompts',
              count: (data.count as number) ?? 0
            },
            timestamp: new Date(),
            threadId
          };

        case 'fanout_dropped':
          return {
            type: 'fanout_dropped',
            data: {
              reason: (data.reason as string) || 'unknown'
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
              maxIterations: (data.max_iterations as number) || 500,
              reason: data.reason as 'max_iterations' | 'repeated_tool_result' | undefined,
              scope: (data.scope as 'main_agent' | 'sub_agent' | undefined),
              agentName: data.agent_name as string | undefined,
              toolCallCount: data.tool_call_count as number | undefined,
              repeatedToolName: data.repeated_tool_name as string | undefined,
              repeatedCount: data.repeated_count as number | undefined,
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
              source: data.source as string | undefined,
              skillName: data.skill_name as string | null | undefined,
              reason: data.reason as string | null | undefined,
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

  private normalizeDispatchInfo(
    value: unknown,
    eventData?: Record<string, unknown>
  ): DispatchInfo | undefined {
    const payload = (value && typeof value === 'object')
      ? value as Record<string, unknown>
      : {};
    const threadId = (
      payload.thread_id ||
      payload.threadId ||
      eventData?.target_thread_id ||
      eventData?.targetThreadId
    ) as string | undefined;
    if (!threadId) return undefined;
    return {
      threadId,
      title: (
        payload.title ||
        eventData?.title ||
        threadId
      ) as string,
      originalThreadId: (
        payload.original_thread_id ||
        payload.originalThreadId ||
        eventData?.original_thread_id ||
        eventData?.originalThreadId
      ) as string | undefined,
      matchedRef: (
        eventData?.matched_ref ||
        eventData?.matchedRef
      ) as string | undefined,
    };
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
}
