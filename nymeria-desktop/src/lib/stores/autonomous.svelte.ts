/**
 * Store for handling autonomous task streaming from the backend.
 *
 * Connects to the /autonomous/stream SSE endpoint to receive real-time
 * updates when Nymeria executes scheduled tasks.
 */

import { configStore } from './config.svelte';
import { clientId } from './clientId.svelte';
import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { activityStore } from './activity.svelte';
import { todosStore } from './todos.svelte';
import { threadConfigStore } from './threadConfig.svelte';
import { notificationStore } from './notifications.svelte';
import { authPromptStore } from './authPrompt.svelte';
import { api } from '$lib/services/api.svelte';
import { debugLog, debugLoggingEnabled } from '$lib/utils/debug';
import { isTodoTool } from '$lib/utils/todoTools';

interface AutonomousEvent {
  type: string;
  thread_id: string;
  task_id: string;
  timestamp: string;
  // Additional fields depend on event type
  [key: string]: unknown;
}

/**
 * Classify the source of an autonomous task from its SSE event data.
 */
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
  'workspace_artifact',
  'response'
]);

const HIGH_VOLUME_EVENT_TYPES = new Set(['response', 'thinking', 'tool_call_delta']);
const BASE_RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;
const IDLE_TIMEOUT_MS = 30000;

debugLog('[Autonomous] Store module loading...');

function createAutonomousStore() {
  debugLog('[Autonomous] Creating store instance');
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
  let eventCounts = new Map<string, number>();
  let totalEventCount = 0;
  let activeTaskId = $state<string | null>(null); // Track which autonomous task we're streaming
  let activeMessageId = $state<string | null>(null); // Track the message we created for this task

  // Multi-thread task tracking
  let activeTasksByThread = $state<Map<string, string>>(new Map()); // thread_id -> task_id
  let activeMessagesByThread = $state<Map<string, string>>(new Map()); // thread_id -> message_id

  // Buffer events that arrive during thread switch gap (between prepareForThreadSwitch
  // clearing isStreaming and the post-history-load recovery re-entering streaming)
  let _pendingEvents = new Map<string, AutonomousEvent[]>();
  let _pendingReplayTimers = new Map<string, ReturnType<typeof setTimeout>>();

  function getStreamUrl(): string {
    const baseUrl = configStore.apiUrl.replace(/\/$/, '');
    const userId = configStore.identity?.id;
    if (!userId) {
      throw new Error('Cannot build stream URL: no identity resolved yet');
    }
    return `${baseUrl}/autonomous/stream?user_id=${encodeURIComponent(userId)}&client_id=${clientId}`;
  }

  function shouldLogEventSample(eventType: string, count: number): boolean {
    if (count <= 3) return true;
    if (HIGH_VOLUME_EVENT_TYPES.has(eventType)) return count % 100 === 0;
    return count % 10 === 0;
  }

  function eventCountSummary(): string {
    const counts = Array.from(eventCounts.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([type, count]) => `${type}:${count}`)
      .join(', ');
    return counts || 'none';
  }

  function logReceivedEvent(event: AutonomousEvent) {
    const count = (eventCounts.get(event.type) || 0) + 1;
    eventCounts.set(event.type, count);
    totalEventCount += 1;

    if (shouldLogEventSample(event.type, count)) {
      debugLog(
        `[Autonomous] Event handled type=${event.type} count=${count} total=${totalEventCount} ` +
        `thread=${event.thread_id || 'none'} task=${event.task_id || 'none'}`
      );
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
      if (runId !== streamRunId || intentionallyDisconnected) return;

      idleAbortReason = 'idle_timeout';
      console.warn(
        `[Autonomous] Stream idle timeout after ${IDLE_TIMEOUT_MS}ms; aborting for reconnect ` +
        `(run=${runId})`
      );
      streamAbortController?.abort();
    }, IDLE_TIMEOUT_MS);
  }

  function catchUpAfterReconnect() {
    const currentThread = threadsStore.currentThreadId;
    if (currentThread && !chatStore.isStreaming) {
      debugLog('[Autonomous] Reconnected - catching up on thread', currentThread);
      api.getThreadHistory(currentThread).then((history) => {
        if (threadsStore.currentThreadId === currentThread && !chatStore.isStreaming) {
          chatStore.setMessages(history.messages);
        }
      }).catch(() => {});
      api.getThreadContextStats(currentThread).then((stats) => {
        if (threadsStore.currentThreadId === currentThread) {
          chatStore.setContextStats(stats);
        }
      }).catch(() => {});
    }

    threadsStore.syncFromBackend();
  }

  function connect() {
    if (streamAbortController || connecting) {
      debugLog('[Autonomous] Stream already active, skipping connect');
      return;
    }

    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    // Don't connect if not configured
    if (!configStore.isConfigured) {
      debugLog('[Autonomous] Not connecting - config not ready (apiUrl:', configStore.apiUrl, 'apiKey present:', !!configStore.apiKey, ')');
      intentionallyDisconnected = false;
      scheduleReconnect('config_not_ready');
      return;
    }

    let url: string;
    try {
      url = getStreamUrl();
    } catch (e) {
      debugLog('[Autonomous] Not connecting - identity not resolved yet');
      intentionallyDisconnected = false;
      scheduleReconnect('identity_not_ready');
      return;
    }

    intentionallyDisconnected = false;
    const runId = ++streamRunId;
    const abortController = new AbortController();
    streamAbortController = abortController;
    connecting = true;
    idleAbortReason = null;
    eventCounts = new Map();
    totalEventCount = 0;

    debugLog(
      `[Autonomous] Connecting to SSE endpoint via fetch (run=${runId}, attempt=${reconnectAttempts + 1}):`,
      url
    );

    void readStream(runId, url, abortController);
  }

  async function readStream(runId: number, url: string, abortController: AbortController) {
    const decoder = new TextDecoder();
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let buffer = '';
    let firstByteSeen = false;
    let firstFrameSeen = false;
    let firstDataEventSeen = false;
    let reconnectReason = 'stream_end';

    function processFrame(frame: string) {
      if (runId !== streamRunId) return;

      if (!firstFrameSeen) {
        firstFrameSeen = true;
        debugLog(
          `[Autonomous] First SSE frame received (run=${runId}, ` +
          `kind=${frame.startsWith(':') ? 'heartbeat' : 'data'})`
        );
      }

      if (!frame.trim() || frame.trimStart().startsWith(':')) {
        return;
      }

      const dataLines = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart());

      if (dataLines.length === 0) {
        return;
      }

      const jsonStr = dataLines.join('\n').trim();
      if (!jsonStr || jsonStr === '[DONE]') {
        return;
      }

      if (!firstDataEventSeen) {
        firstDataEventSeen = true;
        debugLog(`[Autonomous] First data event frame received (run=${runId})`);
      }

      try {
        const data: AutonomousEvent = JSON.parse(jsonStr);
        handleEvent(data);
      } catch (e) {
        console.error('[Autonomous] Failed to parse event:', e, jsonStr);
      }
    }

    try {
      armIdleTimer(runId);
      const response = await fetch(url, {
        method: 'GET',
        headers: {
          Accept: 'text/event-stream',
          Authorization: `Bearer ${configStore.apiKey}`
        },
        signal: abortController.signal
      });

      debugLog(
        `[Autonomous] SSE HTTP status ${response.status} ${response.statusText || ''} (run=${runId})`
      );

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        reconnectReason = `http_${response.status}`;
        throw new Error(`Autonomous stream HTTP ${response.status}: ${text}`);
      }

      if (!response.body) {
        reconnectReason = 'no_body';
        throw new Error('Autonomous stream returned no response body');
      }

      const wasDisconnected = !connected;
      connected = true;
      connecting = false;
      reconnectAttempts = 0;
      debugLog(`[Autonomous] SSE connection established successfully (run=${runId})`);

      if (wasDisconnected) {
        catchUpAfterReconnect();
      }

      reader = response.body.getReader();
      streamReader = reader;

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          reconnectReason = 'stream_end';
          console.warn(`[Autonomous] SSE stream ended by server (run=${runId})`);
          break;
        }

        if (!firstByteSeen) {
          firstByteSeen = true;
          debugLog(
            `[Autonomous] First stream byte received (run=${runId}, bytes=${value.byteLength})`
          );
        }

        armIdleTimer(runId);
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, '\n');

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
        console.warn(`[Autonomous] SSE stream aborted after idle timeout (run=${runId})`);
      } else if ((e as Error)?.name === 'AbortError') {
        reconnectReason = 'aborted';
        debugLog(`[Autonomous] SSE stream aborted (run=${runId})`);
      } else {
        reconnectReason = reconnectReason === 'stream_end' ? 'error' : reconnectReason;
        console.error('[Autonomous] SSE stream error:', e);
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
        debugLog(
          `[Autonomous] Stream closed (run=${runId}, reason=${reconnectReason}, events={${eventCountSummary()}})`
        );

        if (!intentionallyDisconnected) {
          scheduleReconnect(reconnectReason);
        }
      }
    }
  }

  function disconnect() {
    intentionallyDisconnected = true;
    streamRunId++;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
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

    for (const timer of _pendingReplayTimers.values()) {
      clearTimeout(timer);
    }
    _pendingReplayTimers.clear();
    connected = false;
    debugLog(`[Autonomous] Stream intentionally disconnected (events={${eventCountSummary()}})`);
  }

  function scheduleReconnect(reason: string) {
    if (intentionallyDisconnected) {
      debugLog('[Autonomous] Not scheduling reconnect - stream was intentionally disconnected');
      return;
    }

    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }

    reconnectAttempts++;
    const delay = Math.min(BASE_RECONNECT_DELAY_MS * reconnectAttempts, MAX_RECONNECT_DELAY_MS);
    debugLog(
      `[Autonomous] Reconnecting in ${delay}ms ` +
      `(attempt ${reconnectAttempts}, reason=${reason})`
    );

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
    const buf = _pendingEvents.get(event.thread_id) || [];
    buf.push(event);
    _pendingEvents.set(event.thread_id, buf);
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

  function ensureStreamingForCurrentTask(event: AutonomousEvent, placeholder = 'Autonomous task in progress...'): boolean {
    const taskId = event.task_id as string | undefined;
    if (!taskId || event.thread_id !== threadsStore.currentThreadId || chatStore.isStreaming) {
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
    chatStore.setIntermediateContent(placeholder);
    return true;
  }

  function canApplyStreamingEvent(event: AutonomousEvent, isCurrentThread: boolean, isOurTask: boolean): boolean {
    return (
      isCurrentThread &&
      isOurTask &&
      chatStore.isStreaming &&
      activeMessagesByThread.has(event.thread_id)
    );
  }

  function replayPendingEventsForThread(threadId: string) {
    if (threadsStore.currentThreadId !== threadId) return;

    const taskId = activeTasksByThread.get(threadId);
    if (taskId) activeTaskId = taskId;

    const pending = _pendingEvents.get(threadId);
    if (!pending || pending.length === 0) return;

    _pendingEvents.delete(threadId);
    for (const evt of pending) {
      handleEvent(evt);
    }
  }

  function handleEvent(event: AutonomousEvent) {
    logReceivedEvent(event);

    const currentThreadId = threadsStore.currentThreadId;
    const isCurrentThread = event.thread_id === currentThreadId;
    // Check both the legacy single-task tracking and per-thread tracking
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
        // Refresh scheduled todos
        todosStore.fetch();
        activityStore.fetch();

        // If this is a trigger task, ensure its thread exists in the sidebar
        if (event.trigger_name) {
          threadsStore.ensureThread(event.thread_id, event.trigger_name as string);
        }

        // Track this task per-thread
        activeTasksByThread = new Map(activeTasksByThread).set(
          event.thread_id, event.task_id as string
        );
        _pendingEvents.delete(event.thread_id); // clear stale buffer from previous task
        threadsStore.setThreadActive(event.thread_id, true);

        // If on the same thread and not already streaming (user typing),
        // show that autonomous activity is starting
        if (isCurrentThread && !chatStore.isStreaming) {
          // Show autonomous prompt if the global toggle is on OR the per-thread
          // override is force-on. Only for scheduler/watchdog/trigger tasks, not
          // callable thread invocations.
          const threadCfg = threadConfigStore.getConfig(event.thread_id);
          const effective =
            configStore.showAutonomousPrompts || Boolean(threadCfg?.showAutonomousPrompts);
          if (effective && event.prompt && !event.callable_name) {
            const sourceLabel = classifyAutonomousSource(event);
            chatStore.addAutonomousPromptMessage(event.prompt as string, sourceLabel);
          }

          ensureStreamingForCurrentTask(event, 'Autonomous task started...');
        }
        break;

      case 'thinking':
        // Only update if this is our autonomous task on the current thread
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.addThinkingStep(event.content as string || 'Thinking...');
        } else if (isCurrentThread && isOurTask) {
          // Buffer during thread switch gap or while user chat is still streaming.
          bufferPendingEvent(event);
        }
        break;

      case 'tool_call_delta':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.flushStreamingBuffers();
          chatStore.setAssistantActivityPhase('formulating');
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
            (event.args as Record<string, unknown>) || {}
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'tool_result':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          const toolId = event.id as string;
          chatStore.updateToolCallStepResult(
            toolId,
            event.result as string || '',
            'success'
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        // Refresh relevant stores based on tool
        if (isTodoTool(event.name as string | undefined)) {
          todosStore.onTodoToolCompleted();
          activityStore.fetch();
        }
        if (event.name === 'self_invoke') {
          // Legacy self_invoke - now handled through TODOs
          todosStore.fetch();
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

      case 'auth_prompt': {
        // Per-user filtering already happens at the SSE generator; opening
        // unconditionally is safe. The modal is global (not thread-scoped):
        // even if the user navigated away from the thread that issued the
        // prompt, they should still be able to complete it.
        const promptId = event.prompt_id as string | undefined;
        if (!promptId) break;
        authPromptStore.open({
          prompt_id: promptId,
          credential_id: (event.credential_id as string) || '',
          provider: (event.provider as string) || '',
          display_name: (event.display_name as string) || (event.provider as string) || '',
          mode: (event.mode as 'api_key' | 'pat' | 'oauth' | 'oauth_device' | 'form') || 'api_key',
          description: (event.description as string) || '',
          instructions: (event.instructions as string) || '',
          fields: (event.fields as Array<{
            name: string;
            label: string;
            secret: boolean;
            kind?: 'password' | 'text' | 'textarea';
            placeholder?: string;
            help?: string;
          }>) || [],
          account_label: (event.account_label as string) || '',
          existing_accounts: (event.existing_accounts as Array<{
            credential_id: string;
            name: string;
            account_label: string | null;
            status: string;
            last_used_at: string | null;
            last_tested_at: string | null;
          }>) || [],
          timeout_seconds: (event.timeout_seconds as number) || 180,
          expires_at: (event.expires_at as string | null | undefined) ?? null,
          // Hosted-form path
          connect_url: (event.connect_url as string | null | undefined) ?? null,
          connect_url_required: (event.connect_url_required as boolean | undefined) ?? undefined,
          connect_url_error: (event.connect_url_error as string | null | undefined) ?? null,
          // OAuth (auth-code + device-code) shared fields
          flow: (event.flow as 'auth_code' | 'device_code' | undefined),
          provider_id: (event.provider_id as string | undefined),
          scopes: (event.scopes as string[] | undefined),
          notes: (event.notes as string | null | undefined) ?? null,
          // Authorization-code fields
          auth_url: (event.auth_url as string | undefined),
          uses_pkce: (event.uses_pkce as boolean | undefined),
          // Device-code (RFC 8628) fields
          user_code: (event.user_code as string | undefined),
          verification_uri: (event.verification_uri as string | undefined),
          verification_uri_complete: (event.verification_uri_complete as string | null | undefined) ?? null,
          expires_in: (event.expires_in as number | undefined),
          interval: (event.interval as number | undefined),
        });
        break;
      }

      case 'auth_prompt_resolved':
      case 'auth_prompt_cancelled': {
        const promptId = event.prompt_id as string | undefined;
        if (promptId) {
          authPromptStore.clearById(promptId);
        }
        break;
      }

      case 'response':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.addResponseStep(event.content as string || '');
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        break;

      case 'task_completed':
        // Always refresh these
        todosStore.fetch();
        activityStore.fetch();
        if (event.notify) {
          notificationStore.fetch();
        }
        refreshThreadTaskCounts();

        // Mark thread as unread if Nymeria finished work on a thread the user isn't viewing
        if (!isCurrentThread && !event.error) {
          threadsStore.markThreadUnread(event.thread_id);
        }

        if (
          isCurrentThread &&
          isOurTask &&
          !activeMessagesByThread.has(event.thread_id) &&
          !chatStore.isStreaming
        ) {
          replayPendingEventsForThread(event.thread_id);
        }
        const hadStreamingMessage = activeMessagesByThread.has(event.thread_id);

        // Clear per-thread tracking
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

        if (isCurrentThread && isOurTask && chatStore.isStreaming && hadStreamingMessage) {
          chatStore.setStreaming(false);

          // Handle error case - task failed
          if (event.error) {
            const errorMsg = (event.error_message as string) || (event.content as string) || 'Task failed';
            debugLog('[Autonomous] Task failed:', errorMsg);
            chatStore.setLastMessageError(errorMsg);
            chatStore.clearActiveToolCalls();
            activeTaskId = null;
            activeMessageId = null;
            break;
          }

          // Reclassify trailing thinking as response (same as regular chat done handler)
          chatStore.reclassifyThinkingAsResponse();

          // Only set content from task_completed if nothing was streamed via steps.
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

        // If we were tracking this task, clear it
        if (isOurTask) {
          activeTaskId = null;
          activeMessageId = null;
        }

        // Reload canonical history to fill gaps from mid-stream thread switch
        if (isCurrentThread) {
          api.getThreadHistory(event.thread_id).then((history) => {
            if (threadsStore.currentThreadId === event.thread_id && !chatStore.isStreaming) {
              chatStore.setMessages(history.messages);
            }
          }).catch(() => {});
        }
        break;

      case 'webhook_message':
        // A message was received on a platform thread (Discord, Telegram, etc.)
        // Show activity indicator on that thread in the sidebar
        if (event.thread_id) {
          threadsStore.setThreadActive(event.thread_id, true);
          // Auto-clear after a short delay
          setTimeout(() => {
            threadsStore.setThreadActive(event.thread_id, false);
          }, 3000);
        }
        break;

      case 'notification':
        notificationStore.fetch();
        break;

      // ================================================================
      // Cross-client sync events (from other frontend instances)
      // ================================================================

      case 'message_added':
        // Another client sent a user message — just touch the thread
        // so it moves to top of sidebar. The polling loop will pick up
        // the actual message content from thread history.
        if (event.thread_id) {
          threadsStore.touchThread(event.thread_id);
        }
        break;

      case 'thread_updated':
        // Another client changed thread metadata or routing.
        {
          const updates: Partial<{
            title: string;
            pinned: boolean;
            platform: import('$lib/types').ThreadPlatform;
          }> = {};
          if (event.title !== undefined) updates.title = event.title as string;
          if (event.pinned !== undefined) updates.pinned = event.pinned as boolean;
          if (event.platform !== undefined) {
            updates.platform = event.platform as import('$lib/types').ThreadPlatform;
          }
          threadsStore.updateThreadFromSync(event.thread_id, updates);
        }
        break;

      case 'thread_created':
        // Another client created a new thread
        threadsStore.addThreadFromSync(
          event.thread_id,
          (event.title as string) || 'New Chat',
          event.platform as import('$lib/types').ThreadPlatform | undefined,
        );
        break;

      case 'thread_deleted':
        // Another client deleted a thread
        threadsStore.deleteThreadLocal(event.thread_id);
        break;

      // ================================================================
      // Mid-turn pending-prompt drain on an autonomous holder.
      // The interactive path handles these in MainPanel.svelte for
      // user-started turns. For TODO/trigger-started turns there is no
      // interactive chatStream, so we have to convert queued prompts
      // into real user bubbles here too.
      // ================================================================
      case 'prompt_injected': {
        if (!isCurrentThread || !isOurTask) break;
        const data = event.data as { count: number; sources?: string[] };
        const injected = chatStore.consumeQueuedPrompts(data.count);
        chatStore.flushStreamingBuffers();
        chatStore.setLastMessageComplete();
        chatStore.clearActiveToolCalls();
        for (const p of injected) {
          chatStore.addUserMessage(p.content);
        }
        chatStore.addAssistantMessage();
        break;
      }

      case 'prompt_queued':
      case 'prompt_absorbed':
      case 'turn_halted':
      case 'fanout_dropped':
        // Lifecycle signals already surfaced by the per-prompt queue stream
        // in queueOnBusyThread(); acknowledged here so the default-branch
        // debugLog doesn't flag them as unknown.
        break;

      default:
        debugLog('[Autonomous] Unknown event type:', event.type);
    }
  }

  return {
    get connected() {
      return connected;
    },
    get reconnectAttempts() {
      return reconnectAttempts;
    },
    get activeTaskId() {
      return activeTaskId;
    },
    get isAutonomousStreaming() {
      return activeTaskId !== null;
    },
    connect,
    disconnect,
    hasActiveTask(threadId: string): boolean {
      return activeTasksByThread.has(threadId);
    },
    getActiveTaskId(threadId: string): string | undefined {
      return activeTasksByThread.get(threadId);
    },
    /** Called after thread switch recovery re-arms streaming. Replays any events
     *  that arrived during the gap and syncs activeTaskId for consistency. */
    resumeStreamingForThread(threadId: string) {
      replayPendingEventsForThread(threadId);
    }
  };
}

export const autonomousStore = createAutonomousStore();

if (debugLoggingEnabled && typeof window !== 'undefined') {
  (window as unknown as { _autonomousStore: typeof autonomousStore })._autonomousStore = autonomousStore;
  debugLog('[Autonomous] Store exposed on window._autonomousStore for debugging');
}
