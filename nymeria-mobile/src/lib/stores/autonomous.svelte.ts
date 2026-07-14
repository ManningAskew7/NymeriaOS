/**
 * Autonomous Store (Mobile)
 *
 * Connects to /autonomous/stream SSE endpoint using fetch + ReadableStream
 * instead of EventSource (which has Android WebView issues).
 */

import { configStore } from './config.svelte';
import { clientId } from './clientId.svelte';
import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { activityStore } from './activity.svelte';
import { todosStore } from './todos.svelte';
import { threadConfigStore } from './threadConfig.svelte';
import { notificationStore } from './notifications.svelte';
import { workflowsStore } from './workflows.svelte';
import { api } from '$lib/services/api.svelte';
import { isTodoTool } from '$lib/utils/todoTools';
import { Network } from '@capacitor/network';
import type { PluginListenerHandle } from '@capacitor/core';

interface AutonomousEvent {
  type: string;
  thread_id: string;
  task_id: string;
  timestamp: string;
  [key: string]: unknown;
}

function classifyAutonomousSource(event: AutonomousEvent): string {
  if (event.todo_id) return 'scheduler';
  if (event.source === 'watchdog') return 'watchdog';
  if (event.trigger_id || event.trigger_name) return 'trigger';
  return 'autonomous';
}

const STREAMING_AUTONOMOUS_EVENT_TYPES = new Set([
  'thinking',
  'tool_call_delta',
  'tool_call',
  'tool_result',
  'tool_reload',
  'provider_retry',
  'provider_fallback',
  'workspace_artifact',
  'response'
]);

const BASE_RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;
const IDLE_TIMEOUT_MS = 30000;

// Per-thread cap on the replay buffer (backlog #89, mirrors the desktop store).
// Streaming events for a thread that is not on screen are buffered so switching
// into it can replay what already streamed. These bounds cover a long
// multi-tool turn while tripping on a runaway turn; on overflow the buffer is
// dropped and the join falls back to the history snapshot (graceful
// degradation to the pre-replay behavior).
export const MAX_BUFFERED_EVENTS_PER_THREAD = 4000;
export const MAX_BUFFERED_CHARS_PER_THREAD = 2_000_000;

export interface PendingBuffer {
  events: AutonomousEvent[];
  chars: number;
  overflowed: boolean;
}

/**
 * Flip a thread's replay buffer to overflowed once it exceeds either bound.
 * Returns true if overflowed (caller should stop appending). The events array
 * is dropped to free memory; the join then replays nothing and falls back to
 * the history snapshot. Pure (module-scoped) so it is unit testable.
 */
export function markBufferOverflowIfNeeded(buf: PendingBuffer, threadId = ''): boolean {
  if (buf.overflowed) return true;
  if (
    buf.events.length >= MAX_BUFFERED_EVENTS_PER_THREAD ||
    buf.chars >= MAX_BUFFERED_CHARS_PER_THREAD
  ) {
    buf.overflowed = true;
    buf.events = [];
    console.warn(
      `[Autonomous] Replay buffer overflow for thread ${threadId} ` +
      `(events>=${MAX_BUFFERED_EVENTS_PER_THREAD} or chars>=${MAX_BUFFERED_CHARS_PER_THREAD}); ` +
      `will fall back to history on join`
    );
    return true;
  }
  return false;
}

function createAutonomousStore() {
  let connected = $state(false);
  let streamAbortController: AbortController | null = null;
  let streamReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let connecting = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempts = $state(0);
  let streamRunId = 0;
  let intentionallyDisconnected = true;
  let idleAbortReason: string | null = null;
  let networkOnline = true;
  let networkListener: PluginListenerHandle | null = null;
  let networkListenerReady = false;
  let activeTaskId = $state<string | null>(null);
  let activeMessageId = $state<string | null>(null);

  // Multi-thread task tracking
  let activeTasksByThread = $state<Map<string, string>>(new Map());
  let activeMessagesByThread = $state<Map<string, string>>(new Map());

  // Buffer events during thread switch gap (bounded per-thread; see
  // markBufferOverflowIfNeeded, backlog #89).
  let _pendingEvents = new Map<string, PendingBuffer>();
  let _pendingReplayTimers = new Map<string, ReturnType<typeof setTimeout>>();

  function getStreamUrl(): string {
    const baseUrl = configStore.apiUrl.replace(/\/$/, '');
    const userId = configStore.identity?.id;
    if (!userId) {
      throw new Error('Cannot build stream URL: no identity resolved yet');
    }
    const params = new URLSearchParams({
      user_id: userId,
      client_id: clientId,
      api_key: configStore.apiKey
    });
    return `${baseUrl}/autonomous/stream?${params}`;
  }

  function redactStreamUrl(url: string): string {
    try {
      const parsed = new URL(url);
      if (parsed.searchParams.has('api_key')) {
        parsed.searchParams.set('api_key', '***');
      }
      return parsed.toString();
    } catch {
      return url.replace(/([?&]api_key=)[^&]*/i, '$1***');
    }
  }

  function clearIdleTimer() {
    if (idleTimer) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function armIdleTimer(runId: number) {
    clearIdleTimer();
    idleTimer = setTimeout(() => {
      if (runId !== streamRunId || intentionallyDisconnected || !networkOnline) return;

      idleAbortReason = 'idle_timeout';
      console.warn(`[Autonomous] Stream idle timeout after ${IDLE_TIMEOUT_MS}ms; reconnecting`);
      streamAbortController?.abort();
    }, IDLE_TIMEOUT_MS);
  }

  function clearReconnectTimer() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function catchUpAfterReconnect() {
    const currentThread = threadsStore.currentThreadId;
    if (currentThread && !chatStore.isStreaming) {
      api.getThreadHistory(currentThread).then((history) => {
        if (threadsStore.currentThreadId === currentThread && !chatStore.isStreaming) {
          chatStore.setMessages(history.messages);
        }
      }).catch(() => {});
      api.getThreadContextStats(currentThread).then((stats) => {
        if (threadsStore.currentThreadId === currentThread) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats?.model ?? null);
        }
      }).catch(() => {});
    }

    threadsStore.syncFromBackend();
  }

  function pauseStreamForOffline() {
    clearReconnectTimer();
    clearIdleTimer();
    streamRunId++;

    try {
      void streamReader?.cancel();
    } catch {
      // Ignore cancel failures; abort below is the authoritative stop signal.
    }

    streamAbortController?.abort();
    streamReader = null;
    streamAbortController = null;
    connecting = false;
    connected = false;
    idleAbortReason = null;
  }

  async function ensureNetworkListener() {
    if (networkListenerReady) return;
    networkListenerReady = true;

    try {
      const status = await Network.getStatus();
      networkOnline = status.connected;
      networkListener = await Network.addListener('networkStatusChange', (status) => {
        networkOnline = status.connected;

        if (!status.connected) {
          console.warn('[Autonomous] Network offline; pausing stream reconnects');
          pauseStreamForOffline();
          return;
        }

        console.log('[Autonomous] Network online; reconnecting stream');
        reconnectAttempts = 0;
        if (!intentionallyDisconnected && !streamAbortController && !connecting) {
          connect();
        }
      });
    } catch {
      networkOnline = true;
      console.log('[Autonomous] Capacitor Network plugin unavailable; using stream errors for reconnects');
    }
  }

  function connect() {
    if (streamAbortController || connecting) return;

    clearReconnectTimer();
    intentionallyDisconnected = false;
    connecting = true;

    void startConnection();
  }

  async function startConnection() {
    await ensureNetworkListener();

    if (intentionallyDisconnected) {
      connecting = false;
      return;
    }

    if (!networkOnline) {
      connecting = false;
      scheduleReconnect('offline');
      return;
    }

    if (!configStore.isConfigured) {
      connecting = false;
      scheduleReconnect('config_not_ready');
      return;
    }

    let url: string;
    try {
      url = getStreamUrl();
    } catch {
      connecting = false;
      scheduleReconnect('identity_not_ready');
      return;
    }

    const runId = ++streamRunId;
    const abortController = new AbortController();
    streamAbortController = abortController;
    idleAbortReason = null;

    console.log(
      `[Autonomous] Connecting to SSE endpoint via fetch (run=${runId}, attempt=${reconnectAttempts + 1}):`,
      redactStreamUrl(url)
    );

    void readStream(runId, url, abortController);
  }

  async function readStream(runId: number, url: string, abortController: AbortController) {
    const decoder = new TextDecoder();
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let buffer = '';
    let reconnectReason = 'stream_end';

    function processFrame(frame: string) {
      if (runId !== streamRunId) return;
      if (!frame.trim() || frame.trimStart().startsWith(':')) return;

      const dataLines = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart());

      if (dataLines.length === 0) return;

      const jsonStr = dataLines.join('\n').trim();
      if (!jsonStr || jsonStr === '[DONE]') return;

      try {
        const event: AutonomousEvent = JSON.parse(jsonStr);
        handleEvent(event);
      } catch (e) {
        console.error('[Autonomous] Parse error:', e, jsonStr);
      }
    }

    try {
      armIdleTimer(runId);
      const response = await fetch(url, {
        signal: abortController.signal,
        headers: {
          'Accept': 'text/event-stream',
          'Authorization': `Bearer ${configStore.apiKey}`
        }
      });

      if (!response.ok) {
        reconnectReason = `http_${response.status}`;
        throw new Error(`HTTP ${response.status}`);
      }

      if (!response.body) {
        reconnectReason = 'no_body';
        throw new Error('Autonomous stream returned no response body');
      }

      const wasDisconnected = !connected;
      connected = true;
      connecting = false;
      reconnectAttempts = 0;
      console.log(`[Autonomous] Stream connected (run=${runId})`);

      if (wasDisconnected) {
        catchUpAfterReconnect();
      }

      reader = response.body.getReader();
      streamReader = reader;

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          reconnectReason = 'stream_end';
          break;
        }

        armIdleTimer(runId);
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

        let frameEnd = buffer.indexOf('\n\n');
        while (frameEnd !== -1) {
          const frame = buffer.slice(0, frameEnd);
          buffer = buffer.slice(frameEnd + 2);
          processFrame(frame);
          frameEnd = buffer.indexOf('\n\n');
        }
      }

      const trailing = `${buffer}${decoder.decode()}`.trim();
      if (trailing) {
        processFrame(trailing);
      }
    } catch (e) {
      if (idleAbortReason === 'idle_timeout') {
        reconnectReason = 'idle_timeout';
        console.warn(`[Autonomous] Stream aborted after idle timeout (run=${runId})`);
      } else if ((e as Error)?.name === 'AbortError') {
        reconnectReason = networkOnline ? 'aborted' : 'offline';
        console.log(`[Autonomous] Stream aborted (run=${runId}, reason=${reconnectReason})`);
      } else {
        reconnectReason = reconnectReason === 'stream_end' ? 'error' : reconnectReason;
        console.error('[Autonomous] Stream error:', e);
      }
    } finally {
      clearIdleTimer();
      try {
        reader?.releaseLock();
      } catch {
        // Ignore release errors after abort/cancel.
      }

      if (runId === streamRunId) {
        streamReader = null;
        streamAbortController = null;
        connecting = false;
        connected = false;
        idleAbortReason = null;

        if (!intentionallyDisconnected) {
          scheduleReconnect(reconnectReason);
        }
      }
    }
  }

  function disconnect() {
    intentionallyDisconnected = true;
    streamRunId++;
    clearReconnectTimer();
    clearIdleTimer();

    try {
      void streamReader?.cancel();
    } catch {
      // Ignore cancel failures; abort below is the authoritative stop signal.
    }

    streamAbortController?.abort();
    streamReader = null;
    streamAbortController = null;
    connecting = false;
    void networkListener?.remove();
    networkListener = null;
    networkListenerReady = false;

    for (const timer of _pendingReplayTimers.values()) {
      clearTimeout(timer);
    }
    _pendingReplayTimers.clear();
    connected = false;
  }

  function scheduleReconnect(reason: string) {
    if (intentionallyDisconnected) return;

    clearReconnectTimer();

    if (!networkOnline) {
      console.log(`[Autonomous] Not scheduling reconnect while offline (reason=${reason})`);
      return;
    }

    reconnectAttempts++;
    const delay = Math.min(BASE_RECONNECT_DELAY_MS * reconnectAttempts, MAX_RECONNECT_DELAY_MS);
    console.log(`[Autonomous] Reconnecting in ${delay}ms (attempt ${reconnectAttempts}, reason=${reason})`);

    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  async function refreshThreadTaskCounts() {
    try {
      const counts = await api.getThreadTaskCounts();
      threadsStore.setThreadTaskCounts(counts);
    } catch (e) {
      console.warn('[Autonomous] Failed to refresh thread task counts:', e);
    }
  }

  function bufferPendingEvent(event: AutonomousEvent) {
    // While the chat panel renders this thread from the turn buffer, bus
    // transcript events are redundant (the buffer carries the full turn):
    // drop them instead of queueing, or the pending replay would repaint
    // the whole turn as a duplicate once the attach ends (backlog #90).
    if (chatStore.bufferAttachedThreadId === event.thread_id) return;
    let buf = _pendingEvents.get(event.thread_id);
    if (!buf) {
      buf = { events: [], chars: 0, overflowed: false };
      _pendingEvents.set(event.thread_id, buf);
    }
    if (buf.overflowed) return;
    buf.events.push(event);
    buf.chars += JSON.stringify(event).length;
    markBufferOverflowIfNeeded(buf, event.thread_id);
    schedulePendingReplay(event.thread_id);
  }

  function schedulePendingReplay(threadId: string) {
    if (_pendingReplayTimers.has(threadId)) return;

    const timer = setTimeout(() => {
      _pendingReplayTimers.delete(threadId);
      replayPendingEventsForThread(threadId);
    }, 250);
    _pendingReplayTimers.set(threadId, timer);
  }

  function ensureStreamingForCurrentTask(event: AutonomousEvent): boolean {
    const taskId = event.task_id as string | undefined;
    // While a thread switch is loading history, the message list is about to be
    // replaced, so do not bind a streaming message yet (that binding would be
    // wiped by the history swap). Live turns are re-rendered by the #87
    // viewer-attach path (navigation's requestViewerAttach); this guard just
    // avoids a bind-then-wipe flicker during the swap (backlog #89).
    if (
      !taskId ||
      event.thread_id !== threadsStore.currentThreadId ||
      chatStore.isStreaming ||
      chatStore.isLoadingHistory
    ) {
      return false;
    }

    activeTaskId = taskId;
    let messageId = activeMessagesByThread.get(event.thread_id);
    if (!messageId) {
      messageId = chatStore.addAssistantMessage();
      activeMessagesByThread = new Map(activeMessagesByThread).set(event.thread_id, messageId);
    }
    activeMessageId = messageId;
    chatStore.setStreaming(true);
    return true;
  }

  function canApplyStreamingEvent(event: AutonomousEvent, isCurrentThread: boolean, isOurTask: boolean): boolean {
    // Stand down while the chat panel renders this thread from the turn
    // buffer (viewer attach / dropped-stream recovery): the replay is the
    // single renderer, and a binding left in activeMessagesByThread from
    // before the attach would otherwise double-render the turn (backlog
    // #90 slice 2). Lifecycle handling (task state, dashboards, thread-list
    // spinners) does not pass through here and keeps working.
    if (chatStore.bufferAttachedThreadId === event.thread_id) return false;
    return (
      isCurrentThread &&
      isOurTask &&
      chatStore.isStreaming &&
      activeMessagesByThread.has(event.thread_id)
    );
  }

  function replayPendingEventsForThread(threadId: string) {
    if (threadsStore.currentThreadId !== threadId) return;
    // Defer while history is loading so the 250ms replay timer does not apply
    // buffered events into a list the history swap is about to replace. Unlike
    // desktop, mobile has no post-load autonomous drain (attachToThread); the
    // #87 viewer-attach path is the authoritative live-turn renderer, and any
    // gap buffer left here is discarded at task end (backlog #89).
    if (chatStore.isLoadingHistory) return;
    // The turn-buffer attach replays the turn from seq 0, so anything queued
    // here (the pre-attach switch gap) is already covered: discard it.
    if (chatStore.bufferAttachedThreadId === threadId) {
      _pendingEvents.delete(threadId);
      return;
    }

    const taskId = activeTasksByThread.get(threadId);
    if (taskId) activeTaskId = taskId;

    const pending = _pendingEvents.get(threadId);
    if (!pending || pending.overflowed || pending.events.length === 0) return;

    const events = pending.events;
    _pendingEvents.delete(threadId);
    for (const evt of events) {
      handleEvent(evt);
    }
  }

  function handleEvent(event: AutonomousEvent) {
    console.log('[Autonomous] Event:', event.type, event);

    const currentThreadId = threadsStore.currentThreadId;
    const isCurrentThread = event.thread_id === currentThreadId;
    let isOurTask = activeTaskId === event.task_id ||
      activeTasksByThread.get(event.thread_id) === event.task_id;
    const isStreamingAutonomousEvent = STREAMING_AUTONOMOUS_EVENT_TYPES.has(event.type);

    if (isCurrentThread && isStreamingAutonomousEvent && event.task_id && !isOurTask) {
      activeTasksByThread = new Map(activeTasksByThread).set(
        event.thread_id, event.task_id as string
      );
      threadsStore.setThreadActive(event.thread_id, true);
      isOurTask = true;
    }

    if (isCurrentThread && isStreamingAutonomousEvent && isOurTask && !chatStore.isStreaming) {
      ensureStreamingForCurrentTask(event);
    }

    switch (event.type) {
      case 'task_started':
        todosStore.fetch();
        activityStore.fetch();

        if (event.trigger_name) {
          threadsStore.ensureThread(event.thread_id, event.trigger_name as string);
        }

        activeTasksByThread = new Map(activeTasksByThread).set(
          event.thread_id, event.task_id as string
        );
        _pendingEvents.delete(event.thread_id);
        threadsStore.setThreadActive(event.thread_id, true);

        if (isCurrentThread && !chatStore.isStreaming) {
          const threadCfg = threadConfigStore.getConfig(event.thread_id);
          const effective =
            configStore.showAutonomousPrompts || Boolean(threadCfg?.showAutonomousPrompts);
          if (effective && event.prompt && !event.callable_name) {
            const sourceLabel = classifyAutonomousSource(event);
            chatStore.addAutonomousPromptMessage(event.prompt as string, sourceLabel);
          }

          ensureStreamingForCurrentTask(event);
        }
        break;

      case 'thinking':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.addThinkingStep(event.content as string || 'Thinking…');
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'provider_retry':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          if (event.rewound) chatStore.rewindLastAssistantToStablePoint();
          chatStore.addProviderStatusStep({
            providerStatus: 'retry',
            provider: event.provider as string | undefined,
            model: event.model as string | undefined,
            attempt: event.attempt as number | undefined,
            maxRetries: event.max_retries as number | undefined,
            delaySeconds: event.delay_seconds as number | undefined,
            reason: event.reason as string | undefined,
            httpStatus: event.http_status as number | null | undefined,
            rewound: event.rewound as boolean | undefined,
            streamChunks: event.stream_chunks as number | undefined,
          });
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'provider_fallback':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          if (event.rewound) chatStore.rewindLastAssistantToStablePoint();
          chatStore.addProviderStatusStep({
            providerStatus: 'fallback',
            fromProvider: event.from_provider as string | undefined,
            fromModel: event.from_model as string | undefined,
            toProvider: event.to_provider as string | undefined,
            toModel: event.to_model as string | undefined,
            holdSeconds: event.hold_seconds as number | undefined,
            expiresAt: event.expires_at as string | null | undefined,
            reason: event.reason as string | undefined,
            httpStatus: event.http_status as number | null | undefined,
            rewound: event.rewound as boolean | undefined,
            streamChunks: event.stream_chunks as number | undefined,
          });
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'tool_call':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          const toolId = (event.id as string) || `${event.name}-${Date.now()}`;
          chatStore.addToolCallStep(
            toolId,
            event.name as string,
            (event.args as Record<string, unknown>) || {},
            typeof event.timeout_seconds === 'number' ? event.timeout_seconds : undefined
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'tool_result':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.updateToolCallStepResult(
            event.id as string,
            event.result as string || '',
            'success',
            typeof event.duration_ms === 'number' ? event.duration_ms : undefined
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        if (isTodoTool(event.name as string | undefined)) {
          todosStore.onTodoToolCompleted();
          activityStore.fetch();
        }
        break;

      case 'tool_reload': {
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          const ttlSeconds = event.ttl_seconds ?? event.ttlSeconds;
          chatStore.handleToolReload(
            (event.tools as string[]) || [],
            (event.ttl as string) || '',
            typeof ttlSeconds === 'number' ? ttlSeconds : null,
            event.source as string | undefined,
            (event.skill_name as string | undefined) || (event.skillName as string | undefined),
            event.reason as string | undefined
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;
      }

      case 'workspace_artifact':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          const toolId = event.tool_call_id as string | undefined;
          const path = event.path as string | undefined;
          const name = event.name as string | undefined;
          if (toolId && path && name) {
            chatStore.addToolCallArtifacts(toolId, [{
              path,
              name,
              mimeType: (event.mime_type as string) || 'application/octet-stream',
              sizeBytes: (event.size_bytes as number) || 0
            }]);
          }
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'response':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.addResponseStep(event.content as string || '');
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'task_completed':
        todosStore.fetch();
        activityStore.fetch();
        if (event.notify) {
          notificationStore.fetch();
        }
        refreshThreadTaskCounts();

        if (
          isCurrentThread &&
          isOurTask &&
          !activeMessagesByThread.has(event.thread_id) &&
          !chatStore.isStreaming
        ) {
          replayPendingEventsForThread(event.thread_id);
        }
        const hadStreamingMessage = activeMessagesByThread.has(event.thread_id);

        {
          const nextTasks = new Map(activeTasksByThread);
          nextTasks.delete(event.thread_id);
          activeTasksByThread = nextTasks;
          const nextMsgs = new Map(activeMessagesByThread);
          nextMsgs.delete(event.thread_id);
          activeMessagesByThread = nextMsgs;
          _pendingEvents.delete(event.thread_id);
        }
        threadsStore.setThreadActive(event.thread_id, false);

        if (
          isCurrentThread &&
          isOurTask &&
          chatStore.isStreaming &&
          hadStreamingMessage &&
          chatStore.bufferAttachedThreadId !== event.thread_id
        ) {
          // The buffer-attach guard mirrors canApplyStreamingEvent: while the
          // turn-buffer replay owns this thread's rendering, a stale
          // activeMessagesByThread binding from before the attach must not
          // let this finalize (or error-paint) mid-attach; the buffer's own
          // done/error event finalizes the message instead.
          chatStore.setStreaming(false);

          if (event.error) {
            const errorMsg = (event.error_message as string) || (event.content as string) || 'Task failed';
            chatStore.setLastMessageError(errorMsg);
            chatStore.clearActiveToolCalls();
            activeTaskId = null;
            activeMessageId = null;
            break;
          }

          chatStore.reclassifyThinkingAsResponse();

          const parsedContent = event.content as string;
          const lastMsg = chatStore.messages[chatStore.messages.length - 1];
          const hasResponseSteps = lastMsg?.steps?.some(s => s.type === 'response');
          if (parsedContent && lastMsg?.role === 'assistant' && !hasResponseSteps) {
            chatStore.addResponseStep(parsedContent);
          }
          chatStore.setLastMessageComplete();
          chatStore.clearActiveToolCalls();
          activeTaskId = null;
          activeMessageId = null;
        }

        if (isOurTask) {
          activeTaskId = null;
          activeMessageId = null;
        }

        if (isCurrentThread) {
          api.getThreadHistory(event.thread_id).then((history) => {
            if (threadsStore.currentThreadId === event.thread_id && !chatStore.isStreaming) {
              chatStore.setMessages(history.messages);
            }
          }).catch(() => {});
        }
        break;

      case 'webhook_message':
        if (event.thread_id) {
          threadsStore.setThreadActive(event.thread_id, true);
          setTimeout(() => {
            threadsStore.setThreadActive(event.thread_id, false);
          }, 3000);
        }
        break;

      case 'notification':
        notificationStore.fetch();
        break;

      // Lifecycle-hook approval holds (require_approval, backlog #77). Flat
      // payload (see the workflow_step note below). The hold pins to the
      // tool-call card by the tool_call id the backend threads end to end;
      // marking is scoped to the open thread (elsewhere, the notification and
      // the /hook approvals surfaces cover it) but clearing is global so a
      // late thread switch never strands stale buttons.
      case 'hook_approval': {
        const recordId = event.record_id as string | undefined;
        if (!recordId) break;
        if (isCurrentThread) {
          chatStore.markToolCallPendingApproval(
            (event.tool_call_id as string) || '',
            {
              recordId,
              prompt: (event.prompt as string) || '',
              expiresAt: (event.expires_at as string) || ''
            }
          );
        }
        break;
      }

      case 'hook_approval_resolved': {
        const recordId = event.record_id as string | undefined;
        if (!recordId) break;
        chatStore.clearToolCallPendingApproval(
          recordId,
          (event.tool_call_id as string) || undefined
        );
        break;
      }

      case 'workflow_approval':
      case 'workflow_approval_resolved':
        // A run suspended on nym.approve, or a suspension was resolved
        // (possibly by another client); refetch the pending list.
        workflowsStore.refreshApprovals();
        break;

      // The autonomous wire is FLAT: the backend spreads the event's data
      // dict into the top level (event_bus.autonomous_event_to_payload), so
      // the payload fields live on the event itself, never under `.data`.
      case 'workflow_step':
        workflowsStore.noteStepEvent(
          event as unknown as import('$lib/types').WorkflowStepEvent
        );
        break;

      case 'workflow_run_finished':
        workflowsStore.noteRunFinished(
          event as unknown as import('$lib/types').WorkflowRunFinishedEvent
        );
        break;
    }
  }

  return {
    get connected() { return connected; },
    get reconnectAttempts() { return reconnectAttempts; },
    get activeTaskId() { return activeTaskId; },
    get isAutonomousStreaming() { return activeTaskId !== null; },
    connect,
    disconnect,
    hasActiveTask(threadId: string): boolean {
      return activeTasksByThread.has(threadId);
    },
    getActiveTaskId(threadId: string): string | undefined {
      return activeTasksByThread.get(threadId);
    },
    resumeStreamingForThread(threadId: string) {
      replayPendingEventsForThread(threadId);
    }
  };
}

export const autonomousStore = createAutonomousStore();
