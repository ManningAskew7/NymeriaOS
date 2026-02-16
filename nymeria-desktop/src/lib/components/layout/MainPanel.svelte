<script lang="ts">
  import ChatContainer from '$lib/components/chat/ChatContainer.svelte';
  import InputBar from '$lib/components/chat/InputBar.svelte';
  import ContextStatusBar from '$lib/components/chat/ContextStatusBar.svelte';
  import { ThreadHeader, ThreadSettingsPanel } from '$lib/components/threads';
  import { Button, Modal } from '$lib/components/common';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { activityStore } from '$lib/stores/activity.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { untrack } from 'svelte';
  import type {
    SSEEvent,
    FileAttachment,
    ContextStats,
    ThreadConfig,
    AttachmentValidationResult
  } from '$lib/types';

  let showThreadSettings = $state(false);
  let showAttachmentWarningModal = $state(false);
  let attachmentValidationResult = $state<AttachmentValidationResult | null>(null);
  let warningSuppressChecked = $state(false);
  let pendingSend = $state<{ message: string; attachments?: FileAttachment[] } | null>(null);

  // Load thread config when thread changes
  $effect(() => {
    const tid = threadsStore.currentThreadId;
    if (tid) {
      untrack(() => threadConfigStore.loadConfig(tid));
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
      chatStore.setStreaming(false);
      chatStore.setLastMessageComplete();
      chatStore.clearActiveToolCalls();
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
      const currentThread = threadsStore.currentThread;
      if (currentThread) {
        threadsStore.deleteThread(currentId);
        threadsStore.setThreadFromApi(backendThreadId, currentThread.title);
      }
    }
  }

  function handleSSEEvent(event: SSEEvent) {
    // Sync thread ID early from any event (not just 'done')
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

        // Refresh TODOs when a todo_* tool completes
        if (data.name.startsWith('todo_')) {
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

      case 'response': {
        // Add response as a step (preserves order with thinking and tool calls)
        const data = event.data as { content: string; isComplete: boolean };
        chatStore.addResponseStep(data.content || '');
        break;
      }

      case 'error': {
        const data = event.data as { message: string };
        chatStore.setLastMessageError(data.message);
        break;
      }

      case 'done': {
        const data = event.data as {
          threadId: string;
          muted?: boolean;
          muteReason?: string;
          contextStats?: ContextStats;
          model?: string;
        };
        console.log('[MainPanel] done event received:', { muted: data.muted, muteReason: data.muteReason });

        // Reclassify any trailing thinking as response (if no tool calls followed it)
        chatStore.reclassifyThinkingAsResponse();

        // Update context stats and active model
        if (data.contextStats) {
          chatStore.setContextStats(data.contextStats);
        }
        if (data.model) {
          chatStore.setActiveModel(data.model);
        }

        // Clear queued state
        chatStore.setQueued(false);

        // If response was muted via mute_response tool, remove the assistant message from chat
        if (data.muted) {
          const messages = chatStore.messages;
          const lastMessage = messages[messages.length - 1];
          console.log('[MainPanel] muted=true, removing message:', {
            messageCount: messages.length,
            lastMessageRole: lastMessage?.role,
            lastMessageId: lastMessage?.id
          });
          if (lastMessage && lastMessage.role === 'assistant') {
            chatStore.removeMessage(lastMessage.id);
            console.log('[MainPanel] Message removed');
          }
        }
        // Note: Thread ID syncing is handled by syncThreadIdFromEvent() called at top of handleSSEEvent
        break;
      }

      case 'queued': {
        // Thread is busy with an autonomous task - show waiting indicator
        const data = event.data as { message: string; holder?: string; heldSeconds?: number };
        chatStore.setQueued(true);
        // Log context for debugging
        if (data.holder) {
          console.log(`[MainPanel] Queued: held by ${data.holder} for ${data.heldSeconds ?? '?'}s`);
        }
        break;
      }

      case 'compacting':
        // No longer used - compacting shown via 'thinking' event in AI bubble
        break;

      case 'compact_result':
        // No longer used - result shown via 'response' event in AI bubble
        break;

      case 'compacted': {
        // Conversation was compacted - clear UI and show notification
        const data = event.data as { messagesRemoved: number; autoResumed: boolean; summary?: string };
        chatStore.handleCompacted(data.messagesRemoved, data.summary);
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

      case 'iteration_limit': {
        // Agent was stopped because it hit the maximum number of steps
        const data = event.data as { message: string; maxIterations: number };
        chatStore.addResponseStep(
          `\n\n---\n**Iteration limit reached (${data.maxIterations} steps).** ` +
          `My task may be incomplete — you can ask me to continue where I left off.`
        );
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

  <div class="chat-area">
    <ChatContainer />
  </div>

  <ContextStatusBar />

  <div class="input-area">
    <InputBar
      onSend={handleSendMessage}
      disabled={chatStore.isStreaming}
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
