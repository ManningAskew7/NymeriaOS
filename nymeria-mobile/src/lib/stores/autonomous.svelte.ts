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
import { notificationStore } from './notifications.svelte';
import { workflowsStore } from './workflows.svelte';
import { api } from '$lib/services/api.svelte';
import { isTodoTool } from '$lib/utils/todoTools';
import { Network } from '@capacitor/network';
import type { PluginListenerHandle } from '@capacitor/core';
import type { ThreadTurnStatus } from '$lib/types';

interface AutonomousEvent {
  type: string;
  thread_id: string;
  task_id: string;
  timestamp: string;
  [key: string]: unknown;
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

    // Intentional disconnect (logout / teardown): drop per-thread tracking so
    // a later connect starts clean. The involuntary reconnect path does NOT
    // call disconnect(), so task tracking survives a reconnect and
    // catchUpAfterReconnect resyncs from history.
    activeTasksByThread = new Map();
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

  /**
   * Ask the chat panel to live-attach to this thread's holder turn (backlog
   * #90 slice 3). The turn buffer attach is the single transcript renderer
   * for autonomous turns, so bus signals (task_started, a late-bound first
   * output event, an SSE reconnect with a task in flight) request an attach
   * instead of painting the transcript from bus chunks. One cheap status
   * fetch resolves the `turn` block the attach needs. No 404 race: every
   * publish site fires task_started from inside its on_chunk callback, and
   * the stream tee records the chunk into the buffer before on_chunk runs
   * (pinned by test_stream_bridge.py), so the buffer exists and is live
   * before the signal reaches any client.
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
      // Best-effort: navigation owns the fallback attach entry point, and
      // task_completed reconciles from history; never break the bus loop on
      // a status fetch.
    }
  }

  function handleEvent(event: AutonomousEvent) {
    console.log('[Autonomous] Event:', event.type, event);

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
        todosStore.fetch();
        activityStore.fetch();

        if (event.trigger_name) {
          threadsStore.ensureThread(event.thread_id, event.trigger_name as string);
        }

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
      // seq 0, so nothing is painted or queued from the bus.
      case 'thinking':
      case 'provider_retry':
      case 'provider_fallback':
      case 'tool_call_delta':
      case 'tool_call':
      case 'tool_reload':
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
        break;

      case 'task_completed':
        todosStore.fetch();
        activityStore.fetch();
        if (event.notify) {
          notificationStore.fetch();
        }
        refreshThreadTaskCounts();

        // Clear per-thread tracking. Transcript finalization is the attach
        // path's job (the buffer's own done/error event ends the turn).
        {
          const nextTasks = new Map(activeTasksByThread);
          nextTasks.delete(event.thread_id);
          activeTasksByThread = nextTasks;
        }
        threadsStore.setThreadActive(event.thread_id, false);

        // Terminal reconcile from canonical history. Mobile keeps this
        // (unlike desktop, whose sync poll owns reconciliation): with no
        // poll, this is the only settle path when the attach never ran
        // (missed signals, a failed status fetch, an unattachable turn).
        // The isStreaming guard keeps it off an attach still in progress.
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
    connect,
    disconnect
  };
}

export const autonomousStore = createAutonomousStore();
