import { configStore } from '$lib/stores/config.svelte';
import type {
  AttachmentValidationResult,
  ChatResponse,
  DispatchInfo,
  FileAttachment,
  SSEEvent,
  QueuedBatch,
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

/** Adopt a promoted queue observer as the active interactive stream. */
export function adoptCurrentStream(controller: AbortController, threadId: string): void {
  if (currentAbortController !== controller) currentAbortController?.abort();
  currentAbortController = controller;
  currentStreamThreadId = threadId;
}

/** Check if there's an active interactive chat stream for the given thread. */
export function hasActiveStreamForThread(threadId: string): boolean {
  return currentAbortController !== null && currentStreamThreadId === threadId;
}

/**
 * Human-readable message for a non-OK chat HTTP response. Backend errors are
 * FastAPI-shaped (`{"detail": "..."}` or `{"detail": {"message": ...}}`, e.g.
 * the per-user rate limit and interactive capacity 429s, the 413 attachment
 * caps), so surface the detail text instead of a raw JSON blob; fall back to
 * the legacy status-plus-body string when the body is not that shape.
 */
function httpErrorMessage(status: number, bodyText: string): string {
  try {
    const detail = (JSON.parse(bodyText) as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (detail && typeof detail === 'object') {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string' && message.trim()) return message;
    }
  } catch {
    // Not JSON; use the raw text below.
  }
  return `API error: ${status} - ${bodyText}`;
}

/** Route turn controls before rendering; true transfers cleanup to recovery. */
export async function consumeTurnStream(
  events: AsyncIterable<SSEEvent>,
  onEvent: (event: SSEEvent) => void,
  onTurnStarted: (turnId: string) => void,
  onReplayGap: (turnId: string) => void
): Promise<boolean> {
  let activeTurnId: string | null = null;
  let dispatched = false;
  for await (const event of events) {
    if (event.type === 'dispatched') dispatched = true;
    if (event.type === 'turn_started') {
      activeTurnId = (event.data as { turnId?: string })?.turnId || null;
      if (activeTurnId) onTurnStarted(activeTurnId);
      continue;
    }
    if (event.type === 'turn_replay_gap') {
      if (dispatched) {
        // The target's buffer cannot be reconciled against caller history.
        // Preserve the inline partial and its existing link to that target.
        onEvent({ ...event, type: 'error', data: {
          message: 'The live stream lost part of this reply. Open the linked thread to view its saved history.',
          code: 'turn_replay_gap'
        } });
        return false;
      }
      const turnId = (event.data as { turnId?: string })?.turnId || activeTurnId;
      if (turnId) {
        onReplayGap(turnId);
        return true;
      }
      onEvent({ ...event, type: 'error', data: {
        message: 'Some reply output is unavailable. Reload this thread to view its saved history.',
        code: 'turn_replay_gap'
      } });
      return false;
    }
    onEvent(event);
  }
  return false;
}

/** Consume a replay without treating an unsaved server error as saved success. */
export async function consumeTurnReplay(
  events: AsyncIterable<SSEEvent>,
  onEvent: (event: SSEEvent) => void,
  onAttach: () => void,
  ownsTurn: () => boolean
): Promise<'finished' | 'failed' | 'reconcile' | 'retry' | 'abandon'> {
  let terminal = false;
  let failed = false;
  try {
    for await (const event of events) {
      if (!ownsTurn()) return 'abandon';
      if (event.type === 'turn_replay_gap') return 'reconcile';
      if (event.type === 'turn_attach') { onAttach(); continue; }
      if (event.type === 'turn_started') continue;
      if (event.type === 'error') {
        const code = (event.data as { code?: string })?.code;
        if (code === 'turn_not_found' || code === 'turn_replay_gap') return 'reconcile';
        if (code === 'reattach_failed') return 'retry';
        failed = true;
        terminal = true;
      }
      if (event.type === 'done') terminal = true;
      onEvent(event);
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') return 'abandon';
    return failed ? 'failed' : 'retry';
  }
  return failed ? 'failed' : terminal ? 'finished' : 'reconcile';
}

export class ChatApi extends CredentialsApi {
  async *chatStream(
    message: string,
    threadId?: string,
    attachments?: FileAttachment[],
    forceUnsupportedAttachments: boolean = false
  ): AsyncGenerator<SSEEvent> {
    const url = `${this.getBaseUrl()}/chat`;

    // Create abort controller for this stream. Keep a local reference so
    // cleanup only clears the module slot when it still belongs to THIS
    // stream (a drained old stream must not clobber a newer stream's
    // controller after a thread switch).
    const abortController = new AbortController();
    currentAbortController = abortController;
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
      signal: abortController.signal
    });

    if (!response.ok) {
      const errorText = await response.text();
      yield {
        type: 'error',
        data: {
          message: httpErrorMessage(response.status, errorText),
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
      if (currentAbortController === abortController) {
        currentAbortController = null;
        currentStreamThreadId = null;
      }
    }
  }

  /**
   * Submit a prompt to a thread that may be mid-turn. Uses its own abort
   * controller (separate from chatStream's module-level one) so cancelling
   * a queued prompt does not abort the primary stream and vice-versa.
   * Queuers yield lifecycle events only. A holder start promotes this stream
   * to full output; the visible panel then adopts its controller for Stop.
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
          message: httpErrorMessage(response.status, errorText),
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
    let promoted = false;
    const preamble: SSEEvent[] = [];
    const lifecycle = new Set(['prompt_queued', 'prompt_injected', 'prompt_absorbed',
      'queued', 'turn_halted', 'fanout_dropped', 'error']);
    const parseLine = (line: string): SSEEvent[] => {
      if (!line.startsWith('data: ')) return [];
      const raw = line.slice(6).trim();
      if (!raw || raw === '[DONE]') return [];
      let event: SSEEvent | null;
      try { event = this.parseSSEEvent(JSON.parse(raw)); }
      catch { return []; } // Incomplete or malformed wire data is not an event.
      if (!event) return [];
      if (!promoted && event.type === 'dispatched') {
        preamble.push(event);
        return [];
      }
      if (event.type === 'turn_replay_gap' && (event.data as { turnId?: string }).turnId) {
        promoted = true;
        return [...preamble.splice(0), event];
      }
      if (event.type === 'turn_started') {
        promoted = true;
        return [event, ...preamble.splice(0)];
      }
      return promoted || lifecycle.has(event.type) ? [event] : [];
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          for (const event of parseLine(line)) {
            if (abortController.signal.aborted) return;
            yield event;
            if (event.type === 'prompt_absorbed' || event.type === 'error' || event.type === 'done') return;
          }
        }
      }
      buffer += decoder.decode();
      for (const event of parseLine(buffer)) yield event;
    } finally {
      try { await reader.cancel(); } catch { /* Transport may already be closed. */ }
      reader.releaseLock();
      if (currentAbortController === abortController) {
        currentAbortController = null;
        currentStreamThreadId = null;
      }
    }
  }

  async withdrawQueuedPrompt(threadId: string, promptId: string): Promise<void> {
    const response = await fetch(`${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/queue/${encodeURIComponent(promptId)}`, {
      method: 'DELETE', headers: this.getHeaders()
    });
    if (response.ok) return;
    const body = await response.text();
    if (response.status === 404) {
      try {
        if (JSON.parse(body)?.detail?.code === 'prompt_not_queued') return;
      } catch { /* Report an unexpected response normally. */ }
    }
    throw new Error(httpErrorMessage(response.status, body));
  }

  /**
   * Re-attach to a thread's in-flight (or just-finished, still-buffered)
   * interactive turn after a connection drop. Replays the turn's buffered
   * events from the start (the caller rebuilds the assistant bubble from the
   * replay), then tails live events until the turn ends. Registers itself as
   * the module-level active stream so stop/abort behave like chatStream.
   *
   * Errors are yielded as `error` events with recovery-aware codes:
   * `turn_not_found` (nothing to attach to) and `turn_replay_gap` (buffer
   * overflow) mean "reconcile via history"; other failures mean "retry".
   */
  async *reattachTurnStream(
    threadId: string,
    turnId?: string
  ): AsyncGenerator<SSEEvent> {
    const params = new URLSearchParams();
    if (turnId) params.set('turn_id', turnId);
    const url =
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/turn/stream` +
      (params.size > 0 ? `?${params.toString()}` : '');

    const abortController = new AbortController();
    currentAbortController = abortController;
    currentStreamThreadId = threadId;

    const releaseModuleSlot = () => {
      if (currentAbortController === abortController) {
        currentAbortController = null;
        currentStreamThreadId = null;
      }
    };

    let response: Response;
    try {
      response = await fetch(url, {
        headers: {
          ...this.getHeaders(),
          Accept: 'text/event-stream'
        },
        signal: abortController.signal
      });
    } catch (error) {
      releaseModuleSlot();
      throw error;
    }

    if (!response.ok) {
      releaseModuleSlot();
      let code = response.status === 410 ? 'turn_replay_gap' : 'turn_not_found';
      let message = `Re-attach failed: ${response.status}`;
      try {
        const detail = (await response.json())?.detail;
        if (detail?.code) code = detail.code;
        if (detail?.message) message = detail.message;
      } catch {
        // Non-JSON error body; keep the status-derived defaults.
      }
      if (response.status !== 404 && response.status !== 410) {
        code = 'reattach_failed';
      }
      yield {
        type: 'error',
        data: { message, code },
        timestamp: new Date(),
        threadId
      };
      return;
    }

    const reader = response.body?.getReader();
    if (!reader) {
      currentAbortController = null;
      currentStreamThreadId = null;
      yield {
        type: 'error',
        data: { message: 'No response body', code: 'reattach_failed' },
        timestamp: new Date(),
        threadId
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
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.slice(6).trim();
          if (jsonStr === '[DONE]') return;
          try {
            const parsed = JSON.parse(jsonStr);
            const event = this.parseSSEEvent(parsed);
            if (event) yield event;
          } catch (e) {
            console.error('Failed to parse re-attach SSE event:', e, jsonStr);
          }
        }
      }
    } finally {
      try {
        // Abandoned mid-tail (e.g. thread switch): tear the connection down
        // instead of leaving the SSE body open until the turn ends.
        await reader.cancel();
      } catch {
        // Stream already closed or errored; nothing to cancel.
      }
      reader.releaseLock();
      releaseModuleSlot();
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

  protected parseQueuedBatch(value: unknown): QueuedBatch | undefined {
    if (!value || typeof value !== 'object') return undefined;
    const batch = value as Record<string, unknown>;
    if (typeof batch.id !== 'string' || !Array.isArray(batch.inputs)) return undefined;
    const inputs = batch.inputs.filter(item => item && typeof item === 'object').map((item, index) => ({
      messageId: typeof item.message_id === 'string' ? item.message_id : undefined,
      promptId: String(item.prompt_id ?? ''), position: Number(item.position) || index + 1,
      source: String(item.source ?? 'user'), sourceLabel: String(item.source_label ?? ''),
      userId: String(item.user_id ?? ''), enqueuedAt: Number(item.enqueued_at) || 0,
      text: String(item.text ?? ''), modelContent: String(item.model_content ?? item.text ?? '')
    }));
    return { id: batch.id, total: Number(batch.total) || inputs.length, inputs };
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

        case 'tool_call_delta':
          return {
            type: 'tool_call_delta',
            data: {},
            timestamp: new Date(),
            threadId
          };

        case 'tool_call':
          // Nymeria sends: { type, id, name, args, started_at, timeout_seconds }
          return {
            type: 'tool_call',
            data: {
              id: (data.id as string) || `${data.name}-${Date.now()}`,
              name: data.name as string,
              arguments: (data.args as Record<string, unknown>) || {},
              timeoutSeconds:
                typeof data.timeout_seconds === 'number' ? data.timeout_seconds : undefined
            },
            timestamp: new Date(),
            threadId
          };

        case 'tool_result':
          // Nymeria sends: { type, id, name, result, started_at, duration_ms }
          return {
            type: 'tool_result',
            data: {
              id: data.id as string | undefined,
              name: data.name as string,
              result: (data.result as string) || '',
              status: 'success' as const,
              durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : undefined
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
              rewound: data.rewound as boolean | undefined,
              streamChunks: data.stream_chunks as number | undefined,
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
              permanent: data.permanent as boolean | undefined,
              expiresAt: data.expires_at as string | null | undefined,
              reason: data.reason as string | undefined,
              httpStatus: data.http_status as number | null | undefined,
              rewound: data.rewound as boolean | undefined,
              streamChunks: data.stream_chunks as number | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'error':
          return {
            type: 'error',
            data: {
              message: (data.content as string) || (data.error as string) || 'The server reported an error but sent no details.',
              code: data.code as string | undefined,
              details: data.details as Record<string, unknown> | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'turn_replay_gap':
          return {
            type: 'turn_replay_gap',
            data: { turnId: data.turn_id as string | undefined },
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
            processing: rawStats.processing as boolean | undefined,
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
              message: (data.content as string) || 'Waiting for autonomous task to finish…',
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
              promptId: data.prompt_id as string | undefined,
              queueThreadId: data.queue_thread_id as string | undefined,
              position: (data.position as number) ?? 0,
              holder: (data.holder as string) || undefined,
              heldSeconds: (data.held_seconds as number) || undefined,
              source: (data.source as string) || 'user'
            },
            timestamp: new Date(),
            threadId
          };

        case 'prompt_injected': {
          // `prompts` (raw texts, index-parallel with `sources`) lets any
          // same-thread client render the injected user bubbles: a viewer
          // or cross-client queuer has no local copy of another client's
          // queued prompt text (backlog #87).
          const rawPrompts = (data.prompts as Array<{
            text?: string;
            prompt_id?: string;
            total?: number;
            source_label?: string;
            user_id?: string;
            enqueued_at?: number;
          }> | undefined) ?? undefined;
          return {
            type: 'prompt_injected',
            data: {
              count: (data.count as number) ?? 1,
              promptIds: data.prompt_ids as string[] | undefined,
              queuedBatch: this.parseQueuedBatch({ id: data.batch_id, total: rawPrompts?.[0]?.total, inputs: rawPrompts }),
              sources: (data.sources as string[]) || [],
              prompts: rawPrompts?.map((p) => ({
                text: p.text ?? '',
                promptId: p.prompt_id,
                sourceLabel: p.source_label ?? '',
                userId: p.user_id ?? '',
                enqueuedAt: p.enqueued_at ?? 0
              }))
            },
            timestamp: new Date(),
            threadId
          };
        }

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
            data: { message: (data.message as string) || 'Compacting thread…' },
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
              // True only for graceful main-agent cap halts; gates the
              // Resume button on the pause card (backlog #27).
              resumable: data.resumable === true,
            },
            timestamp: new Date(),
            threadId
          };

        case 'turn_resumed':
          // A /resume re-drive of a halted turn started (this client or
          // another); flips the pause card to its resumed state.
          return {
            type: 'turn_resumed',
            data: {
              toolCallOffset: data.tool_call_offset as number | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'turn_rewound':
          // A pre-output provider refusal (Fable 5 safety classifier) was
          // rewound server-side (backlog #105): the refused exchange is gone
          // from the checkpoint. Truncate the local transcript, restore the
          // prompt to the composer, and show the explanation. Nothing is
          // auto-resent.
          return {
            type: 'turn_rewound',
            data: {
              content: (data.content as string) || '',
              prompt: (data.prompt as string) || '',
              toMessageId: (data.to_message_id as string) || '',
              reason: (data.reason as string) || 'refusal',
              model: (data.model as string) || '',
              // Autonomous refusals are rewound server-side only; the GUI does
              // no transcript surgery or composer restore for them.
              autonomous: (data.autonomous as boolean) || false,
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

        case 'hook_activity':
          // Ephemeral lifecycle-hook activity line (nothing persisted); the
          // backend only emits meaningful runs (a deny/modify/inject or a fault).
          return {
            type: 'hook_activity',
            data: {
              name: data.name as string | undefined,
              event: data.event as string | undefined,
              status: data.status as string | undefined,
              detail: data.detail as string | undefined,
              toolName: data.tool_name as string | null | undefined,
            },
            timestamp: new Date(),
            threadId
          };

        case 'turn_started':
          // Holder-turn identity marker: carries the turn_id used to
          // re-attach to this turn after a connection drop.
          return {
            type: 'turn_started',
            data: { turnId: (data.turn_id as string) || '' },
            timestamp: new Date(),
            threadId
          };

        case 'turn_attach':
          // Re-attach stream preamble (GET /threads/{id}/turn/stream).
          return {
            type: 'turn_attach',
            data: {
              turnId: (data.turn_id as string) || '',
              state: (data.state as string) || 'live',
              lastSeq: (data.last_seq as number) ?? 0,
              truncated: Boolean(data.truncated)
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
