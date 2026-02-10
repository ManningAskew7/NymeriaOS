/**
 * Store for handling autonomous task streaming from the backend.
 *
 * Connects to the /autonomous/stream SSE endpoint to receive real-time
 * updates when Nymeria executes scheduled tasks.
 */

import { configStore } from './config.svelte';
import { chatStore } from './chat.svelte';
import { threadsStore } from './threads.svelte';
import { activityStore } from './activity.svelte';
import { todosStore } from './todos.svelte';
import { api } from '$lib/services/api.svelte';

interface AutonomousEvent {
  type: string;
  thread_id: string;
  task_id: string;
  timestamp: string;
  // Additional fields depend on event type
  [key: string]: unknown;
}

// Debug: log when module loads
console.log('[Autonomous] Store module loading...');

function createAutonomousStore() {
  console.log('[Autonomous] Creating store instance');
  let connected = $state(false);
  let eventSource: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempts = $state(0);
  let activeTaskId = $state<string | null>(null); // Track which autonomous task we're streaming
  let activeMessageId = $state<string | null>(null); // Track the message we created for this task

  // Multi-thread task tracking
  let activeTasksByThread = $state<Map<string, string>>(new Map()); // thread_id -> task_id
  let activeMessagesByThread = $state<Map<string, string>>(new Map()); // thread_id -> message_id

  const MAX_RECONNECT_ATTEMPTS = 10;
  const RECONNECT_DELAY_MS = 3000;

  function getStreamUrl(): string {
    const baseUrl = configStore.apiUrl.replace(/\/$/, '');
    return `${baseUrl}/autonomous/stream?user_id=default`;
  }

  function connect() {
    if (eventSource) {
      console.log('[Autonomous] Already connected, skipping');
      return; // Already connected
    }

    // Don't connect if not configured
    if (!configStore.isConfigured) {
      console.log('[Autonomous] Not connecting - config not ready (apiUrl:', configStore.apiUrl, 'apiKey present:', !!configStore.apiKey, ')');
      scheduleReconnect();
      return;
    }

    const url = getStreamUrl();
    console.log('[Autonomous] Connecting to SSE endpoint:', url);

    // Note: EventSource doesn't support custom headers, so we can't send the API key
    // The backend should handle this via query param or cookie for SSE
    // For now, we'll add the API key as a query param
    const urlWithAuth = `${url}&api_key=${configStore.apiKey}`;

    try {
      eventSource = new EventSource(urlWithAuth);
      console.log('[Autonomous] EventSource created, waiting for connection...');
    } catch (e) {
      console.error('[Autonomous] Failed to create EventSource:', e);
      scheduleReconnect();
      return;
    }

    eventSource.onopen = () => {
      console.log('[Autonomous] SSE connection established successfully');
      connected = true;
      reconnectAttempts = 0;
    };

    eventSource.onmessage = (event) => {
      if (!event.data || event.data.startsWith(':')) {
        // Heartbeat or comment, ignore
        return;
      }

      try {
        const data: AutonomousEvent = JSON.parse(event.data);
        handleEvent(data);
      } catch (e) {
        console.error('[Autonomous] Failed to parse event:', e, event.data);
      }
    };

    eventSource.onerror = (error) => {
      // EventSource errors are often opaque, check readyState for more info
      const state = eventSource?.readyState;
      const stateStr = state === 0 ? 'CONNECTING' : state === 1 ? 'OPEN' : state === 2 ? 'CLOSED' : 'UNKNOWN';
      console.error('[Autonomous] Connection error, readyState:', stateStr, 'error:', error);
      connected = false;
      disconnect();
      scheduleReconnect();
    };
  }

  function disconnect() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    connected = false;
  }

  function scheduleReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }

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

  function handleEvent(event: AutonomousEvent) {
    console.log('[Autonomous] Event:', event.type, event);

    const currentThreadId = threadsStore.currentThreadId;
    const isCurrentThread = event.thread_id === currentThreadId;
    // Check both the legacy single-task tracking and per-thread tracking
    const isOurTask = activeTaskId === event.task_id ||
      activeTasksByThread.get(event.thread_id) === event.task_id;

    switch (event.type) {
      case 'task_started':
        // Refresh scheduled todos
        todosStore.fetch();
        activityStore.fetch();

        // Track this task per-thread
        activeTasksByThread = new Map(activeTasksByThread).set(
          event.thread_id, event.task_id as string
        );
        threadsStore.setThreadActive(event.thread_id, true);

        // If on the same thread and not already streaming (user typing),
        // show that autonomous activity is starting
        if (isCurrentThread && !chatStore.isStreaming) {
          activeTaskId = event.task_id;
          // Add a placeholder message for the autonomous task and track its ID
          activeMessageId = chatStore.addAssistantMessage();
          activeMessagesByThread = new Map(activeMessagesByThread).set(
            event.thread_id, activeMessageId!
          );
          chatStore.setStreaming(true);
          chatStore.setIntermediateContent('Autonomous task started...');
        }
        break;

      case 'thinking':
        // Only update if this is our autonomous task on the current thread
        if (isCurrentThread && isOurTask && chatStore.isStreaming) {
          chatStore.addThinkingStep(event.content as string || 'Thinking...');
        }
        break;

      case 'tool_call':
        if (isCurrentThread && isOurTask && chatStore.isStreaming) {
          const toolId = (event.id as string) || `${event.name}-${Date.now()}`;
          chatStore.addToolCallStep(
            toolId,
            event.name as string,
            (event.args as Record<string, unknown>) || {}
          );
        }
        // Refresh todos if it's a todo tool
        if ((event.name as string)?.startsWith('todo_')) {
          todosStore.onTodoToolCompleted();
        }
        break;

      case 'tool_result':
        if (isCurrentThread && isOurTask && chatStore.isStreaming) {
          const toolId = event.id as string;
          chatStore.updateToolCallStepResult(
            toolId,
            event.result as string || '',
            'success'
          );
        }
        // Refresh relevant stores based on tool
        if ((event.name as string)?.startsWith('todo_')) {
          todosStore.onTodoToolCompleted();
          activityStore.fetch();
        }
        if (event.name === 'self_invoke') {
          // Legacy self_invoke - now handled through TODOs
          todosStore.fetch();
          activityStore.fetch();
        }
        break;

      case 'response':
        if (isCurrentThread && isOurTask && chatStore.isStreaming) {
          chatStore.addResponseStep(event.content as string || '');
        }
        break;

      case 'task_completed':
        // Always refresh these
        todosStore.fetch();
        activityStore.fetch();
        refreshThreadTaskCounts();

        // Clear per-thread tracking
        {
          const nextTasks = new Map(activeTasksByThread);
          nextTasks.delete(event.thread_id);
          activeTasksByThread = nextTasks;
          const nextMsgs = new Map(activeMessagesByThread);
          nextMsgs.delete(event.thread_id);
          activeMessagesByThread = nextMsgs;
        }
        threadsStore.setThreadActive(event.thread_id, false);

        if (isCurrentThread && isOurTask && chatStore.isStreaming) {
          chatStore.setStreaming(false);

          // Handle error case - task failed
          if (event.error) {
            const errorMsg = (event.error_message as string) || (event.content as string) || 'Task failed';
            console.log('[Autonomous] Task failed:', errorMsg);
            chatStore.setLastMessageError(errorMsg);
            chatStore.clearActiveToolCalls();
            activeTaskId = null;
            activeMessageId = null;
            break;
          }

          // Check visibility - if "activity", remove the message from chat
          // (it will only appear in the activity log)
          if (event.visibility === 'activity' && activeMessageId) {
            console.log('[Autonomous] Visibility is "activity", removing message from chat');
            chatStore.removeMessage(activeMessageId);
          } else {
            // Visibility is "full", keep the message in chat
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
          }

          chatStore.clearActiveToolCalls();
          activeTaskId = null;
          activeMessageId = null;
        }

        // If we were tracking this task, clear it
        if (isOurTask) {
          activeTaskId = null;
          activeMessageId = null;
        }

        // If visibility is "full" and user is on a different thread,
        // we could show a notification or indicator
        if (!isCurrentThread && event.visibility === 'full') {
          console.log('[Autonomous] Task completed on different thread:', event.thread_id);
        }
        break;

      default:
        console.log('[Autonomous] Unknown event type:', event.type);
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
    disconnect
  };
}

export const autonomousStore = createAutonomousStore();

// Debug: expose on window for console testing
if (typeof window !== 'undefined') {
  (window as unknown as { _autonomousStore: typeof autonomousStore })._autonomousStore = autonomousStore;
  console.log('[Autonomous] Store exposed on window._autonomousStore for debugging');
}
