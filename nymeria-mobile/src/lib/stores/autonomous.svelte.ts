/**
 * Autonomous Store (Mobile)
 *
 * Connects to /autonomous/stream SSE endpoint using fetch + ReadableStream
 * instead of EventSource (which has Android WebView issues).
 */

import { configStore } from './config.svelte';
import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { activityStore } from './activity.svelte';
import { todosStore } from './todos.svelte';
import { threadConfigStore } from './threadConfig.svelte';
import { notificationStore } from './notifications.svelte';
import { api } from '$lib/services/api.svelte';

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
  'workspace_artifact',
  'response'
]);

function createAutonomousStore() {
  let connected = $state(false);
  let abortController: AbortController | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempts = $state(0);
  let activeTaskId = $state<string | null>(null);
  let activeMessageId = $state<string | null>(null);

  // Multi-thread task tracking
  let activeTasksByThread = $state<Map<string, string>>(new Map());
  let activeMessagesByThread = $state<Map<string, string>>(new Map());

  // Buffer events during thread switch gap
  let _pendingEvents = new Map<string, AutonomousEvent[]>();
  let _pendingReplayTimers = new Map<string, ReturnType<typeof setTimeout>>();

  const MAX_RECONNECT_ATTEMPTS = 10;
  const RECONNECT_DELAY_MS = 3000;

  function getStreamUrl(): string {
    const baseUrl = configStore.apiUrl.replace(/\/$/, '');
    const userId = configStore.identity?.id;
    if (!userId) {
      throw new Error('Cannot build stream URL: no identity resolved yet');
    }
    const params = new URLSearchParams({ user_id: userId, api_key: configStore.apiKey });
    return `${baseUrl}/autonomous/stream?${params}`;
  }

  async function connect() {
    if (abortController) return;

    if (!configStore.isConfigured) {
      scheduleReconnect();
      return;
    }

    let url: string;
    try {
      url = getStreamUrl();
    } catch {
      scheduleReconnect();
      return;
    }
    console.log('[Autonomous] Connecting via fetch:', url);

    abortController = new AbortController();

    try {
      const response = await fetch(url, {
        signal: abortController.signal,
        headers: {
          'Accept': 'text/event-stream',
          'Authorization': `Bearer ${configStore.apiKey}`
        }
      });

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`);
      }

      connected = true;
      reconnectAttempts = 0;
      console.log('[Autonomous] Stream connected');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE lines
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.trim() || line.startsWith(':')) continue;

          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const event: AutonomousEvent = JSON.parse(data);
              handleEvent(event);
            } catch (e) {
              console.error('[Autonomous] Parse error:', e, data);
            }
          }
        }
      }

      // Stream ended cleanly
      connected = false;
      abortController = null;
      scheduleReconnect();
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        return; // Intentional disconnect
      }
      console.error('[Autonomous] Stream error:', e);
      connected = false;
      abortController = null;
      scheduleReconnect();
    }
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    for (const timer of _pendingReplayTimers.values()) {
      clearTimeout(timer);
    }
    _pendingReplayTimers.clear();
    connected = false;
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);

    if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
      console.error('[Autonomous] Max reconnect attempts reached');
      return;
    }

    reconnectAttempts++;
    const delay = RECONNECT_DELAY_MS * reconnectAttempts;
    console.log(`[Autonomous] Reconnecting in ${delay}ms (attempt ${reconnectAttempts})`);

    reconnectTimer = setTimeout(() => {
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
          if (threadCfg?.showAutonomousPrompts && event.prompt && !event.callable_name) {
            const sourceLabel = classifyAutonomousSource(event);
            chatStore.addAutonomousPromptMessage(event.prompt as string, sourceLabel);
          }

          ensureStreamingForCurrentTask(event, 'Autonomous task started...');
        }
        break;

      case 'thinking':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.addThinkingStep(event.content as string || 'Thinking...');
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
        if ((event.name as string)?.startsWith('todo')) {
          todosStore.onTodoToolCompleted();
        }
        break;

      case 'tool_result':
        if (canApplyStreamingEvent(event, isCurrentThread, isOurTask)) {
          chatStore.updateToolCallStepResult(
            event.id as string,
            event.result as string || '',
            'success'
          );
        } else if (isCurrentThread && isOurTask) {
          bufferPendingEvent(event);
        }
        if ((event.name as string)?.startsWith('todo')) {
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

        if (isCurrentThread && isOurTask && chatStore.isStreaming && hadStreamingMessage) {
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
