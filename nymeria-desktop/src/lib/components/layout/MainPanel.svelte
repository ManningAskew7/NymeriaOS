<script lang="ts">
  import ChatContainer from '$lib/components/chat/ChatContainer.svelte';
  import InputBar from '$lib/components/chat/InputBar.svelte';
  import ContextStatusBar from '$lib/components/chat/ContextStatusBar.svelte';
  import QuickActions from '$lib/components/outlook/QuickActions.svelte';
  import { ThreadHeader, ThreadSettingsPanel } from '$lib/components/threads';
  import { Button, Modal } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { activityStore } from '$lib/stores/activity.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { outlookStore } from '$lib/stores/outlook.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { api } from '$lib/services/api.svelte';
  import { debugLog } from '$lib/utils/debug';
  import { isTodoTool } from '$lib/utils/todoTools';
  import { refreshThreadSyncBaseline } from '$lib/stores/syncPoll.svelte';
  import { untrack } from 'svelte';
  import type {
    SSEEvent,
    FileAttachment,
    ContextStats,
    ThreadConfig,
    AttachmentValidationResult,
    DispatchInfo
  } from '$lib/types';

  let showThreadSettings = $state(false);
  let showAttachmentWarningModal = $state(false);
  let pendingInsertText = $state('');
  let attachmentValidationResult = $state<AttachmentValidationResult | null>(null);
  let warningSuppressChecked = $state(false);
  let pendingSend = $state<{ message: string; attachments?: FileAttachment[] } | null>(null);

  // Load global stores for thread header badges
  $effect(() => {
    if (configStore.isConfigured) {
      untrack(() => {
        if (!defaultToolsStore.loaded && !defaultToolsStore.loading) defaultToolsStore.load();
        if (!serverSettingsStore.loaded && !serverSettingsStore.loading) serverSettingsStore.load();
        if (!triggersStore.loaded && !triggersStore.loading) triggersStore.loadTriggers();
      });
    }
  });

  // Load thread config when thread changes
  $effect(() => {
    const tid = threadsStore.currentThreadId;
    const thread = threadsStore.currentThread;
    if (tid && !thread?.recovered) {
      untrack(() => {
        threadConfigStore.loadConfig(tid).catch((err) => {
          console.warn('[MainPanel] Failed to load thread config:', err);
        });
      });
    }
  });

  const currentThreadConfig = $derived(
    threadsStore.currentThreadId
      ? threadConfigStore.getConfig(threadsStore.currentThreadId) ?? null
      : null
  );

  function handleConfigSaved(config: ThreadConfig) {
    // Config is already in the store via updateConfig/deleteConfig
  }

  function closeAttachmentWarningModal() {
    showAttachmentWarningModal = false;
    attachmentValidationResult = null;
    warningSuppressChecked = false;
    pendingSend = null;
  }

  async function streamMessage(
    message: string,
    attachments?: FileAttachment[],
    forceUnsupportedAttachments: boolean = false
  ) {
    if ((!message.trim() && (!attachments || attachments.length === 0)) || chatStore.isStreaming) return;

    // Ensure a thread exists before sending — prevents the backend from
    // generating a fallback ID that triggers syncThreadIdFromEvent's replace logic
    if (!threadsStore.currentThreadId) {
      threadsStore.createThread();
    }

    // Auto-title the thread from the first message if it's still "New Chat"
    const currentThread = threadsStore.currentThread;
    if (currentThread && currentThread.title === 'New Chat' && message.trim()) {
      threadsStore.autoTitleFromMessage(currentThread.id, message);
    }

    // Add user message with optional attachments
    chatStore.addUserMessage(message, attachments);

    // Create assistant message placeholder
    chatStore.addAssistantMessage();
    chatStore.setStreaming(true);

    const threadId = threadsStore.currentThreadId || undefined;

    try {
      for await (const event of api.chatStream(message, threadId, attachments, forceUnsupportedAttachments)) {
        handleSSEEvent(event);
      }
    } catch (error) {
      // Check if this was an intentional abort (user clicked stop)
      if (error instanceof DOMException && error.name === 'AbortError') {
        // User stopped - already handled in stopGenerating()
        return;
      }
      console.error('Chat error:', error);
      chatStore.setLastMessageError(
        error instanceof Error ? error.message : 'Unknown error occurred'
      );
    } finally {
      // Only touch chat store if we're still on the stream's original thread —
      // a thread switch during streaming replaces messages, so touching them here
      // would corrupt the new thread's state.
      if (threadsStore.currentThreadId === threadId) {
        chatStore.setStreaming(false);
        chatStore.setLastMessageComplete();
        chatStore.clearActiveToolCalls();
        // Update sync poll baseline so it doesn't re-fetch what we just streamed
        if (threadId) void refreshThreadSyncBaseline(threadId);
      }
    }
  }

  async function handleConfirmUnsupportedSend() {
    const send = pendingSend;
    if (!send) {
      closeAttachmentWarningModal();
      return;
    }

    if (warningSuppressChecked) {
      configStore.suppressAttachmentWarnings = true;
    }

    closeAttachmentWarningModal();
    await streamMessage(send.message, send.attachments, true);
  }

  async function handleSendMessage(message: string, attachments?: FileAttachment[]) {
    if ((!message.trim() && (!attachments || attachments.length === 0)) || chatStore.isStreaming) return;

    if (attachments && attachments.length > 0) {
      if (configStore.suppressAttachmentWarnings) {
        await streamMessage(message, attachments, true);
        return;
      }

      const threadId = threadsStore.currentThreadId || 'preview';
      try {
        const validation = await api.validateThreadAttachments(threadId, attachments);
        if (!validation.compatible) {
          attachmentValidationResult = validation;
          pendingSend = { message, attachments };
          warningSuppressChecked = false;
          showAttachmentWarningModal = true;
          return;
        }
      } catch (e) {
        console.warn('Attachment preflight validation failed; proceeding without preflight:', e);
      }
    }

    await streamMessage(message, attachments);
  }

  /**
   * Returns true if the thread ID belongs to a non-desktop platform
   * or a callable agent thread — these must never be replaced by
   * syncThreadIdFromEvent.
   */
  function isNonDesktopThread(id: string): boolean {
    return (
      id.startsWith('trigger-') ||
      id.startsWith('discord_') ||
      id.startsWith('telegram_') ||
      id.startsWith('slack_') ||
      id.startsWith('twitch_') ||
      id.startsWith('agent-') ||
      id.startsWith('spawned-')
    );
  }

  /**
   * Sync thread ID from any SSE event.
   * This ensures the frontend knows the backend's thread ID even if the stream is stopped early.
   */
  function syncThreadIdFromEvent(event: SSEEvent) {
    const backendThreadId = event.threadId;
    if (!backendThreadId) return;

    const currentId = threadsStore.currentThreadId;

    // If we don't have a local thread, create one with backend's ID
    if (!currentId) {
      const firstUserMessage = chatStore.messages.find((m) => m.role === 'user');
      const title = firstUserMessage?.content || 'New Chat';
      threadsStore.setThreadFromApi(backendThreadId, title);
      threadsStore.autoTitleFromMessage(backendThreadId, title);
      return;
    }

    // If we have a local thread with different ID, update it to use backend's ID
    if (currentId !== backendThreadId) {
      // Guard: never replace a desktop thread with a trigger/system thread
      // and never replace a trigger thread with another trigger thread.
      // Only allow replacement for backend-generated 8-char fallback IDs.
      if (isNonDesktopThread(backendThreadId) || isNonDesktopThread(currentId)) {
        return;
      }

      const currentThread = threadsStore.currentThread;
      if (currentThread) {
        threadsStore.deleteThread(currentId);
        threadsStore.setThreadFromApi(backendThreadId, currentThread.title);
      }
    }
  }

  function handleSSEEvent(event: SSEEvent) {
    // Guard: skip events from a different thread (cross-thread SSE pollution).
    // Must run BEFORE syncThreadIdFromEvent to prevent destructive thread replacement.
    // When currentThreadId is null (initial sync case), allow through.
    if (event.threadId && threadsStore.currentThreadId && event.threadId !== threadsStore.currentThreadId) {
      return;
    }

    // Sync thread ID from any event (not just 'done')
    // This ensures context is preserved even if user stops generation
    syncThreadIdFromEvent(event);

    switch (event.type) {
      case 'thinking': {
        // Add thinking as a step (preserves order with tool calls)
        // Backend sends 'content', but we support 'message' for backwards compatibility
        const data = event.data as { content?: string; message?: string };
        chatStore.addThinkingStep(data.content || data.message || '');
        break;
      }

      case 'tool_call_delta': {
        chatStore.flushStreamingBuffers();
        chatStore.setAssistantActivityPhase('formulating');
        break;
      }

      case 'tool_call': {
        // Add tool call as a step (preserves order with thinking)
        const data = event.data as {
          id: string;
          name: string;
          arguments: Record<string, unknown>;
        };
        chatStore.addToolCallStep(data.id, data.name, data.arguments);

        // Refresh scheduled todos when self_invoke is called (legacy)
        if (data.name === 'self_invoke') {
          todosStore.fetch();
        }
        break;
      }

      case 'tool_result': {
        // Update the tool call step with its result
        const data = event.data as {
          id?: string;
          name: string;
          result: string;
          status: 'success' | 'error';
        };
        if (data.id) {
          chatStore.updateToolCallStepResult(data.id, data.result, data.status);
        } else {
          // Fallback: use legacy method if no ID provided
          chatStore.updateToolCallResultByName(data.name, data.result, data.status, data.id);
        }

        // Refresh TODOs when a TODO tool completes.
        if (isTodoTool(data.name)) {
          todosStore.onTodoToolCompleted();
          // Also refresh activity since todo changes are logged
          activityStore.fetch();
        }

        // Refresh scheduled todos when self_invoke completes (legacy)
        if (data.name === 'self_invoke') {
          todosStore.fetch();
          activityStore.fetch();
        }
        break;
      }

      case 'workspace_artifact': {
        const data = event.data as {
          toolCallId?: string;
          artifact: import('$lib/types').WorkspaceArtifact;
        };
        if (data.toolCallId) {
          chatStore.addToolCallArtifacts(data.toolCallId, [data.artifact]);
        }
        break;
      }

      case 'response': {
        // Add response as a step (preserves order with thinking and tool calls)
        const data = event.data as { content: string; isComplete: boolean };
        chatStore.addResponseStep(data.content || '');
        break;
      }

      case 'dispatched': {
        chatStore.setLastAssistantDispatchInfo(event.data as DispatchInfo);
        break;
      }

      case 'error': {
        const data = event.data as {
          message: string;
          code?: string;
          details?: Record<string, unknown>;
        };
        const message = data.code
          ? `${data.message}\n\n(code: ${data.code})`
          : data.message;
        chatStore.setLastMessageError(message);
        break;
      }

      case 'done': {
        const data = event.data as {
          threadId: string;
          contextStats?: ContextStats;
          model?: string;
          title?: string;
          title_source?: string;
          dispatchedTo?: DispatchInfo;
        };

        // Reclassify any trailing thinking as response (if no tool calls followed it)
        chatStore.reclassifyThinkingAsResponse();

        // Update context stats and active model
        if (!data.dispatchedTo && data.contextStats) {
          chatStore.setContextStats(data.contextStats);
        }
        if (!data.dispatchedTo && data.model) {
          chatStore.setActiveModel(data.model);
        }

        // Apply backend-generated title (auto-title from first message)
        if (data.title && data.dispatchedTo?.threadId) {
          threadsStore.applyBackendTitle(data.dispatchedTo.threadId, data.title);
        } else if (data.title && data.threadId) {
          threadsStore.applyBackendTitle(data.threadId, data.title);
        }

        // Clear queued state
        chatStore.setQueued(false);
        // Note: Thread ID syncing is handled by syncThreadIdFromEvent() called at top of handleSSEEvent
        break;
      }

      case 'queued': {
        // Thread is busy with an autonomous task - show waiting indicator
        const data = event.data as { message: string; holder?: string; heldSeconds?: number };
        chatStore.setQueued(true);
        // Log context for debugging
        if (data.holder) {
          debugLog(`[MainPanel] Queued: held by ${data.holder} for ${data.heldSeconds ?? '?'}s`);
        }
        break;
      }

      case 'compacting': {
        const data = event.data as { message: string };
        chatStore.setCompacting(true, data.message);
        break;
      }

      case 'compact_result':
        // No longer used - result shown via 'response' event in AI bubble
        break;

      case 'compacted': {
        // Conversation was compacted - clear UI and show notification
        const data = event.data as { messagesRemoved: number; autoResumed: boolean; summary?: string };
        chatStore.handleCompacted(data.messagesRemoved, data.summary, data.autoResumed);
        break;
      }

      case 'context_attached': {
        // Attach context summary to the last user message for collapsible display
        const data = event.data as { summary: string };
        if (data.summary) {
          chatStore.setLastUserMessageContextSummary(data.summary);
        }
        break;
      }

      case 'tool_reload': {
        const data = event.data as {
          tools: string[];
          ttl: string;
          ttlSeconds: number | null;
          source?: string;
          skillName?: string | null;
          reason?: string | null;
        };
        chatStore.handleToolReload(data.tools, data.ttl, data.ttlSeconds, data.source, data.skillName, data.reason);
        break;
      }

      case 'iteration_limit': {
        // Agent was stopped because it hit the maximum number of steps
        const data = event.data as {
          message: string;
          maxIterations: number;
          reason?: 'max_iterations' | 'repeated_tool_result';
          scope?: 'main_agent' | 'sub_agent';
          agentName?: string;
          toolCallCount?: number;
          repeatedToolName?: string;
          repeatedCount?: number;
        };

        if (data.scope === 'sub_agent') {
          const agentName = data.agentName || 'Sub-agent';
          const countText = data.toolCallCount
            ? `${data.toolCallCount}/${data.maxIterations}`
            : `${data.maxIterations}`;
          const title = data.reason === 'repeated_tool_result'
            ? `${agentName} stopped a repeated tool loop`
            : `${agentName} hit its iteration limit`;
          chatStore.addResponseStep(
            `\n\n---\n**${title} (${countText} steps).** ` +
            `${data.message || 'The sub-agent was stopped before finishing.'}`
          );
        } else if (data.reason === 'repeated_tool_result') {
          chatStore.addResponseStep(
            `\n\n---\n**Repeated tool loop stopped.** ` +
            `${data.message || 'The agent repeated the same tool call and result too many times.'}`
          );
        } else {
          chatStore.addResponseStep(
            `\n\n---\n**Iteration limit reached (${data.maxIterations} steps).** ` +
            `${data.message || 'My task may be incomplete. You can ask me to continue where I left off.'}`
          );
        }
        break;
      }
    }
  }
</script>

<div class="main-panel-content">
  {#if threadsStore.currentThread}
    <ThreadHeader
      thread={threadsStore.currentThread}
      threadConfig={currentThreadConfig}
      onOpenSettings={() => (showThreadSettings = true)}
    />
  {/if}

  {#if outlookStore.isOutlook}
    <QuickActions
      onAction={(msg) => handleSendMessage(msg)}
      onInsert={(text) => { pendingInsertText = text; }}
      disabled={chatStore.isStreaming}
    />
  {/if}

  <div class="chat-area">
    <ChatContainer />
  </div>

  <ContextStatusBar />

  <div class="input-area">
    <InputBar
      onSend={handleSendMessage}
      disabled={chatStore.isStreaming}
      insertText={pendingInsertText}
      onInsertConsumed={() => { pendingInsertText = ''; }}
      placeholder={chatStore.isQueued
        ? 'Waiting for autonomous task to finish...'
        : chatStore.isStreaming
          ? 'Waiting for response...'
          : 'Type a message...'}
    />
  </div>
</div>

{#if showThreadSettings && threadsStore.currentThread}
  <ThreadSettingsPanel
    thread={threadsStore.currentThread}
    threadConfig={currentThreadConfig}
    onClose={() => (showThreadSettings = false)}
    onSaved={handleConfigSaved}
  />
{/if}

<Modal
  title="Attachment Compatibility Warning"
  isOpen={showAttachmentWarningModal}
  onClose={closeAttachmentWarningModal}
>
  <div class="attachment-warning-modal">
    <p>
      The current model <code>{attachmentValidationResult?.effective_model || 'unknown'}</code>
      is likely incompatible with one or more attached files.
    </p>

    {#if attachmentValidationResult?.unsupported_modalities.length}
      <p>
        Unsupported modalities:
        <strong>{attachmentValidationResult.unsupported_modalities.join(', ')}</strong>
      </p>
    {/if}

    {#if attachmentValidationResult?.warnings.length}
      <ul class="warning-list">
        {#each attachmentValidationResult.warnings as warning}
          <li>{warning}</li>
        {/each}
      </ul>
    {/if}

    <p class="warning-note">
      Sending anyway will likely return an API error.
    </p>

    <label class="suppress-warning">
      <input type="checkbox" bind:checked={warningSuppressChecked} />
      <span>Don't show this warning again</span>
    </label>

    <div class="warning-actions">
      <Button variant="ghost" onclick={closeAttachmentWarningModal}>
        Cancel
      </Button>
      <Button variant="danger" onclick={handleConfirmUnsupportedSend}>
        Send anyway
      </Button>
    </div>
  </div>
</Modal>

<style>
  .main-panel-content {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-base);
  }

  .chat-area {
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }

  .input-area {
    padding: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
  }

  .attachment-warning-modal {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .attachment-warning-modal p {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .warning-list {
    margin: 0;
    padding-left: var(--spacing-lg);
    color: var(--warning);
  }

  .warning-note {
    color: var(--error);
    font-weight: 500;
  }

  .suppress-warning {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .warning-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }
</style>
