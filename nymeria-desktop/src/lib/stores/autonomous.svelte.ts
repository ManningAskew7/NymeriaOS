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
import { notificationStore } from './notifications.svelte';
import { workflowsStore } from './workflows.svelte';
import { authPromptStore } from './authPrompt.svelte';
import { uiPromptStore } from './uiPrompt.svelte';
import { errorsStore } from './errors.svelte';
import { refreshThreadSyncBaseline } from './syncPoll.svelte';
import { api } from '$lib/services/api.svelte';
import { debugLog, debugLoggingEnabled } from '$lib/utils/debug';
import { isTodoTool } from '$lib/utils/todoTools';
import type { ThreadTurnStatus } from '$lib/types';
import {
  isSkillMutationReloadSource,
  isSkillMutationToolName,
  refreshSkillStateAfterMutation
} from '$lib/utils/skillRefresh';

interface AutonomousEvent {
  type: string;
  thread_id: string;
  task_id: string;
  timestamp: string;
  // Additional fields depend on event type
  [key: string]: unknown;
}

/**
 * Reload the on-screen transcript after another client rewound this thread.
 * If the user was mid-edit, the target bubble no longer resolves after a
 * reload (hydration assigns fresh ids), so the edit is cancelled with an
 * explanatory toast rather than risking a rewind against removed messages.
 */
async function reloadAfterExternalRewind(threadId: string): Promise<void> {
  const editingId = chatStore.editingMessageId;
  try {
    const history = await api.getThreadHistory(threadId);
    if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;
    chatStore.setMessages(history.messages);
    void refreshThreadSyncBaseline(threadId);
  } catch (error) {
    debugLog('Failed to reload thread after external rewind:', error);
    return;
  }
  if (editingId && !chatStore.messages.some((m) => m.id === editingId)) {
    chatStore.cancelEdit();
    errorsStore.push({
      kind: 'generic',
      message: 'This conversation was rewound from another device, so your edit was cancelled.',
    });
  }
}

// Turn-output events drive LIFECYCLE only (task late-binding, thread-list
// spinners, the live-attach signal): the turn buffer attach path is the
// single transcript renderer for autonomous turns (backlog #90 slice 3),
// so these events are never painted into the chat view from the bus.
const TURN_OUTPUT_EVENT_TYPES = new Set([
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

const HIGH_VOLUME_EVENT_TYPES = new Set(['response', 'thinking', 'tool_call_delta']);
const BASE_RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT_DELAY_MS = 30000;
const IDLE_TIMEOUT_MS = 30000;

/**
 * Attach-gate for autonomous turn signals (backlog #90 slice 3): a bus
 * signal (task_started, a late-bound first output event, an SSE reconnect
 * with a task in flight) hands the on-screen thread to the viewer attach
 * only when the turn is live and replayable and this client is not already
 * rendering it (own interactive turn or an attach in progress). A truncated
 * buffer cannot replay, so attaching would park the panel on "Reconnecting";
 * skip and let history reconciliation settle the turn instead. Pure
 * (module-scoped) so it is unit testable with the real rule.
 */
export function shouldRequestViewerAttach(
  turn: ThreadTurnStatus | null | undefined,
  isCurrentThread: boolean,
  isStreaming: boolean
): turn is ThreadTurnStatus {
  return Boolean(
    isCurrentThread &&
    !isStreaming &&
    turn &&
    turn.state === 'live' &&
    !turn.truncated
  );
}

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

  // Multi-thread task tracking: thread_id -> task_id for in-flight autonomous
  // turns. Lifecycle only (thread-list spinners via setThreadActive, the
  // late-binding dedupe, task_completed cleanup); transcript rendering is
  // owned by the turn buffer attach path (backlog #90 slice 3).
  let activeTasksByThread = $state<Map<string, string>>(new Map());

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
          // If a turn is still in flight on this thread, re-join it through
          // the turn buffer attach (its replay-from-seq-0 supersedes the
          // history snapshot just applied); a task registered before the
          // disconnect gets no fresh task_started, so this is its re-entry.
          if (activeTasksByThread.has(currentThread)) {
            void requestAttachForLiveTurn(currentThread);
          }
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

    // Intentional disconnect (logout / teardown): drop per-thread tracking so
    // a later connect starts clean. The involuntary reconnect path does NOT
    // call disconnect(), so task tracking survives a reconnect and
    // catchUpAfterReconnect resyncs from history.
    activeTasksByThread = new Map();
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

  /**
   * Ask the chat panel to live-attach to this thread's holder turn (backlog
   * #90 slice 3). The turn buffer attach is the single transcript renderer
   * for autonomous turns, so bus signals (task_started, a late-bound first
   * output event, an SSE reconnect with a task in flight) request an attach
   * instead of painting the transcript from bus chunks. One cheap status
   * fetch resolves the `turn` block the attach needs (mirrors syncPoll's
   * proven shape). No 404 race: every publish site fires task_started from
   * inside its on_chunk callback, and the stream tee records the chunk into
   * the buffer before on_chunk runs (pinned by test_stream_bridge.py), so
   * the buffer exists and is live before the signal reaches any client.
   */
  async function requestAttachForLiveTurn(threadId: string) {
    if (threadsStore.currentThreadId !== threadId || chatStore.isStreaming) return;
    try {
      const status = await api.getThreadStatus(threadId);
      const isCurrentThread = threadsStore.currentThreadId === threadId;
      if (shouldRequestViewerAttach(status.turn, isCurrentThread, chatStore.isStreaming)) {
        chatStore.requestViewerAttach(threadId, status.turn);
      }
    } catch {
      // Best-effort: navigation and the sync poll own the fallback attach
      // entry points; never break the bus loop on a status fetch.
    }
  }

  function handleEvent(event: AutonomousEvent) {
    logReceivedEvent(event);

    // Fanout-mirror events (backend stream_bridge fanout marker) replay a
    // holder turn under a queuer's task id. The holder's own task drives
    // lifecycle here, so drop mirrors before they double spinners, task
    // registration, or dashboard bookkeeping.
    if (event.fanout) return;

    const currentThreadId = threadsStore.currentThreadId;
    const isCurrentThread = event.thread_id === currentThreadId;
    const isRegisteredTask =
      activeTasksByThread.get(event.thread_id) === event.task_id;
    const isTurnOutputEvent = TURN_OUTPUT_EVENT_TYPES.has(event.type);

    // Late-bind the task for ANY thread on its first turn-output event, not
    // just the current one. A background thread (or one whose task_started we
    // missed after an SSE reconnect) must be registered so the thread-list
    // spinner is reliable. For the on-screen thread this doubles as the
    // attach trigger when the task_started signal itself was missed.
    // setThreadActive is thread-keyed and safe ungated.
    if (isTurnOutputEvent && event.task_id && !isRegisteredTask) {
      activeTasksByThread = new Map(activeTasksByThread).set(
        event.thread_id, event.task_id as string
      );
      threadsStore.setThreadActive(event.thread_id, true);
      if (isCurrentThread) {
        void requestAttachForLiveTurn(event.thread_id);
      }
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
        threadsStore.setThreadActive(event.thread_id, true);

        // Turn started on the thread the user is looking at: watch it live
        // through the turn buffer attach (backlog #90 slice 3). The wakeup
        // prompt bubble is no longer painted client-side; the attach anchors
        // to history, which carries it with show_autonomous_prompts honored
        // server-side (hidden stub vs real bubble, autonomous_source badge
        // included).
        if (isCurrentThread) {
          void requestAttachForLiveTurn(event.thread_id);
        }
        break;

      // Turn-output transcript events: consumed for lifecycle only (the
      // late-binding above). Rendering is owned by the turn buffer attach
      // path (backlog #90 slice 3); the buffer replays the full turn from
      // seq 0, so nothing is painted or queued from the bus. Acknowledged
      // here so the default-branch debugLog does not flag them as unknown.
      case 'thinking':
      case 'provider_retry':
      case 'provider_fallback':
      case 'tool_call_delta':
      case 'tool_call':
      case 'workspace_artifact':
      case 'response':
        break;

      case 'tool_result':
        // Dashboard side effects keyed off the tool name (rendering is the
        // attach path's job).
        if (isTodoTool(event.name as string | undefined)) {
          todosStore.onTodoToolCompleted();
          activityStore.fetch();
        }
        if (event.name === 'self_invoke') {
          // Legacy self_invoke - now handled through TODOs
          todosStore.fetch();
          activityStore.fetch();
        }
        if (isSkillMutationToolName(event.name as string | undefined)) {
          refreshSkillStateAfterMutation(event.thread_id);
        }
        break;

      case 'tool_reload':
        if (isSkillMutationReloadSource(event.source as string | undefined)) {
          refreshSkillStateAfterMutation(event.thread_id);
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

      case 'auth_prompt_resolved': {
        const promptId = event.prompt_id as string | undefined;
        if (promptId) {
          authPromptStore.resolveById(promptId, {
            ok: true,
            status: (event.status as string | undefined) || 'active',
            message: (event.message as string | null | undefined) ?? null,
            credentialId: (event.credential_id as string | null | undefined) ?? null,
          });
        }
        break;
      }

      case 'auth_prompt_cancelled': {
        const promptId = event.prompt_id as string | undefined;
        if (!promptId) break;
        const reason = (event.reason as string | undefined) || 'cancelled';
        if (reason === 'cancelled' || reason === 'user_exited' || reason === 'user_message') {
          authPromptStore.clearById(promptId);
        } else {
          authPromptStore.resolveById(promptId, {
            ok: false,
            status: reason,
            message: (event.message as string | null | undefined) ?? null,
            credentialId: null,
          });
        }
        break;
      }

      case 'ui_prompt': {
        // Per-user filtering already happens at the SSE generator. Like
        // auth_prompt, the modal is global (not thread-scoped): the user can
        // answer even after navigating away from the issuing thread.
        const promptId = event.prompt_id as string | undefined;
        if (!promptId) break;
        uiPromptStore.open({
          prompt_id: promptId,
          thread_id: event.thread_id,
          title: (event.title as string) || '',
          html: (event.html as string) || '',
          timeout_seconds: (event.timeout_seconds as number) || 300,
          expires_at: (event.expires_at as string | null | undefined) ?? null,
        });
        break;
      }

      case 'ui_prompt_result': {
        // Any client answered (or the backend published a timeout/abort
        // closure); retract this client's copy of the modal.
        const promptId = event.prompt_id as string | undefined;
        if (promptId) uiPromptStore.clearById(promptId);
        break;
      }

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

        // Clear per-thread tracking. Transcript finalization is the attach
        // path's job (the buffer's own done/error event ends the turn), and
        // the sync poll reconciles the on-screen thread from history if this
        // client never attached (backlog #90 slice 3).
        {
          const nextTasks = new Map(activeTasksByThread);
          nextTasks.delete(event.thread_id);
          activeTasksByThread = nextTasks;
        }
        threadsStore.setThreadActive(event.thread_id, false);
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
      // Lifecycle-hook approval holds (require_approval, backlog #77)
      // ================================================================

      // Flat payload (see the workflow_step note below). The hold pins to the
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

      // ================================================================
      // LLM fallback consent prompts (llm-fallback-consent Phase 2)
      // ================================================================

      // A consent-capable turn parked before a model switch (transport
      // failure or classifier refusal). Same scoping as hook_approval:
      // the card renders only for the open thread (elsewhere, the in-app
      // notification and /fallback approvals cover it), while resolution
      // clears globally so a late thread switch never strands live buttons.
      case 'fallback_prompt': {
        const recordId = event.record_id as string | undefined;
        if (!recordId) break;
        if (isCurrentThread) {
          chatStore.handleFallbackPrompt({
            recordId,
            kind: (event.kind as string) || 'transport',
            fromProvider: (event.from_provider as string) || '',
            fromModel: (event.from_model as string) || '',
            toProvider: (event.to_provider as string) || '',
            toModel: (event.to_model as string) || '',
            reason: (event.reason as string) || '',
            httpStatus: (event.http_status as number | null | undefined) ?? null,
            timeoutSeconds: (event.timeout_seconds as number | null | undefined) ?? null,
            holdOptions: Array.isArray(event.hold_options)
              ? (event.hold_options as number[])
              : [],
            allowPermanent: event.allow_permanent !== false,
            defaultHoldSeconds:
              (event.default_hold_seconds as number | null | undefined) ?? null,
            createdAt: (event.created_at as string) || '',
            expiresAt: (event.expires_at as string) || ''
          });
        }
        break;
      }

      case 'fallback_prompt_resolved': {
        const recordId = event.record_id as string | undefined;
        if (!recordId) break;
        chatStore.resolveFallbackPromptCard(recordId, {
          outcome: (event.outcome as string) || 'stale',
          holdSeconds: (event.hold_seconds as number | null | undefined) ?? null,
          holdPermanent: event.hold_permanent === true
        });
        break;
      }

      // ================================================================
      // Workflow runtime events (nym runs and approvals)
      // ================================================================

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
          (event.title as string) || 'New Thread',
          event.platform as import('$lib/types').ThreadPlatform | undefined,
        );
        break;

      case 'thread_deleted':
        // Another client deleted a thread
        threadsStore.deleteThreadLocal(event.thread_id);
        break;

      case 'thread_teams_changed':
        // Team entities or membership changed backend-side (agent team_manage,
        // the nym.threads.configure verb, a teamed spawn, or another client's
        // team edit): refetch the sidebar's team groupings.
        void threadsStore.loadThreadTeams();
        break;

      case 'thread_rewound': {
        // Another client rewound this thread (edit/rewind affordance or CLI
        // /undo). Our own rewinds apply optimistically before the echo
        // arrives (hook_approval idempotency model), so skip those; only
        // reload the transcript when it is on screen and idle. A live
        // stream owns the transcript; the sync poller reconciles after.
        if ((event._origin_client_id as string | undefined) === clientId) break;
        if (event.thread_id !== threadsStore.currentThreadId) break;
        if (chatStore.isStreaming) break;
        void reloadAfterExternalRewind(event.thread_id);
        break;
      }

      case 'queue_restored': {
        // Another client stopped this thread and the backend handed the
        // queued user prompts back (backlog #16). The stopping client got
        // the texts in its stop response; here we hand OUR locally-queued
        // texts back to our composer and clear the queued bar so nothing
        // looks pending anymore. Own stops are handled in stopGenerating.
        if ((event._origin_client_id as string | undefined) === clientId) break;
        if (event.thread_id !== threadsStore.currentThreadId) break;
        chatStore.restoreLocalPendingToComposer();
        break;
      }

      case 'prompt_injected':
      case 'prompt_queued':
      case 'prompt_absorbed':
      case 'turn_halted':
      case 'fanout_dropped':
        // Queue-lifecycle signals already surfaced elsewhere: the per-prompt
        // queue stream in queueOnBusyThread() handles queue transitions, and
        // the turn buffer carries prompt_injected so the attach path's
        // interactive handler converts queued prompts into user bubbles.
        // Acknowledged here so the default-branch debugLog doesn't flag
        // them as unknown.
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
    connect,
    disconnect
  };
}

export const autonomousStore = createAutonomousStore();

if (debugLoggingEnabled && typeof window !== 'undefined') {
  (window as unknown as { _autonomousStore: typeof autonomousStore })._autonomousStore = autonomousStore;
  debugLog('[Autonomous] Store exposed on window._autonomousStore for debugging');
}
