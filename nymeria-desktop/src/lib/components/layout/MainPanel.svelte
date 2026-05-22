<script lang="ts">
  import ChatContainer from '$lib/components/chat/ChatContainer.svelte';
  import InputBar from '$lib/components/chat/InputBar.svelte';
  import ContextStatusBar from '$lib/components/chat/ContextStatusBar.svelte';
  import QueuedPromptsBar from '$lib/components/chat/QueuedPromptsBar.svelte';
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
  import { uiStore } from '$lib/stores/ui.svelte';
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

  let bothSidebarsOpen = $derived(!uiStore.sidebarCollapsed && !uiStore.rightPanelCollapsed);

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

  // Slash commands whose execution_kind is `chat_stream` on the backend must
  // be routed through the /chat SSE endpoint, not /commands/execute (which
  // rejects them with "handled outside the command service"). The chat-stream
  // intercept in nymeria/api/routers/chat.py handles their state work + agent
  // kickoff in one round-trip. Keep this list in sync with the
  // `execution_kind="chat_stream"` registrations in command_service.py.
  // TODO: make this data-driven via api.listCommands() with execution_kind.
  const CHAT_STREAM_COMMAND_ROOTS = new Set(['/compact', '/orchestrate', '/goal', '/skill', '/kit']);

  async function handleSendMessage(message: string, attachments?: FileAttachment[]) {
    if (!message.trim() && (!attachments || attachments.length === 0)) return;

    const trimmed = message.trim();
    const slashRoot = trimmed.startsWith('/') ? trimmed.split(/\s+/)[0].toLowerCase() : '';
    const isChatStreamCommand = CHAT_STREAM_COMMAND_ROOTS.has(slashRoot);

    // While streaming: queue the prompt sub-turn-style. Slash commands and
    // attachments cannot be queued (backend rejects), so fall back to today's
    // "do nothing" gate for those cases.
    if (chatStore.isStreaming) {
      if (attachments && attachments.length > 0) return;
      if (trimmed.startsWith('/') && !isChatStreamCommand) return;
      if (!threadsStore.currentThreadId) return;
      void queueOnBusyThread(trimmed);
      return;
    }

    if (trimmed.startsWith('/') && !isChatStreamCommand && (!attachments || attachments.length === 0)) {
      if (!threadsStore.currentThreadId) {
        threadsStore.createThread();
      }
      const threadId = threadsStore.currentThreadId || undefined;
      try {
        const result = await api.executeCommand(trimmed, threadId);
        chatStore.addCommandResult(trimmed, result.markdown, result.success);
      } catch (error) {
        chatStore.addCommandResult(
          trimmed,
          `**Error:** ${error instanceof Error ? error.message : 'Command failed'}`,
          false
        );
      }
      return;
    }

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
   * Send a follow-up prompt that the agent should pick up at its next sub-turn
   * halt. Adds the prompt to chatStore.pendingPrompts and POSTs in the
   * background using the queue lifecycle endpoint. Lifecycle events update the
   * prompt's status; the primary stream's `prompt_injected` handler converts
   * it into a real user message at injection time.
   */
  async function queueOnBusyThread(message: string) {
    const threadId = threadsStore.currentThreadId;
    if (!threadId) return;
    const promptId = chatStore.addPendingPrompt(message);
    const controller = new AbortController();
    chatStore.registerPendingPromptAbort(promptId, controller);
    try {
      for await (const event of api.queuePromptStream(message, threadId, controller)) {
        switch (event.type) {
          case 'prompt_queued': {
            const data = event.data as { position: number };
            chatStore.setPendingPromptStatus(promptId, 'queued', undefined, data.position);
            break;
          }
          case 'prompt_absorbed':
            // primary stream already converted this prompt to a user message
            chatStore.removePendingPrompt(promptId);
            return;
          case 'error': {
            const data = event.data as { message: string; code?: string };
            chatStore.setPendingPromptStatus(promptId, 'error', data.message);
            return;
          }
          // queued / turn_halted / fanout_dropped / prompt_injected: ignore
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      const msg = err instanceof Error ? err.message : 'Queue request failed';
      chatStore.setPendingPromptStatus(promptId, 'error', msg);
    }
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
      id.startsWith('matrix_') ||
      id.startsWith('whatsapp_') ||
      id.startsWith('messenger_') ||
      id.startsWith('instagram_') ||
      id.startsWith('webex_') ||
      id.startsWith('mattermost_') ||
      id.startsWith('zulip_') ||
      id.startsWith('rocketchat_') ||
      id.startsWith('teams_') ||
      id.startsWith('googlechat_') ||
      id.startsWith('line_') ||
      id.startsWith('signal_') ||
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

      case 'provider_retry': {
        const data = event.data as {
          provider?: string;
          model?: string;
          attempt?: number;
          maxRetries?: number;
          delaySeconds?: number;
          reason?: string;
          httpStatus?: number | null;
        };
        chatStore.addProviderStatusStep({
          providerStatus: 'retry',
          provider: data.provider,
          model: data.model,
          attempt: data.attempt,
          maxRetries: data.maxRetries,
          delaySeconds: data.delaySeconds,
          reason: data.reason,
          httpStatus: data.httpStatus,
        });
        break;
      }

      case 'provider_fallback': {
        const data = event.data as {
          fromProvider?: string;
          fromModel?: string;
          toProvider?: string;
          toModel?: string;
          holdSeconds?: number;
          expiresAt?: string | null;
          reason?: string;
          httpStatus?: number | null;
        };
        chatStore.addProviderStatusStep({
          providerStatus: 'fallback',
          fromProvider: data.fromProvider,
          fromModel: data.fromModel,
          toProvider: data.toProvider,
          toModel: data.toModel,
          holdSeconds: data.holdSeconds,
          expiresAt: data.expiresAt,
          reason: data.reason,
          httpStatus: data.httpStatus,
        });
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

      case 'turn_halted': {
        const data = event.data as { reason: string; count: number };
        debugLog(`[MainPanel] turn_halted reason=${data.reason} count=${data.count}`);
        break;
      }

      case 'prompt_injected': {
        // Holder is draining queued prompts. Close the current assistant
        // bubble, materialize each queued prompt as a user message in FIFO
        // order, and open a new assistant placeholder for the sub-turn.
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

      case 'prompt_absorbed':
      case 'fanout_dropped':
        break;

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

  <div class="input-area" class:both-open={bothSidebarsOpen}>
    <QueuedPromptsBar />
    <InputBar
      onSend={handleSendMessage}
      disabled={false}
      insertText={pendingInsertText}
      onInsertConsumed={() => { pendingInsertText = ''; }}
      placeholder={chatStore.isQueued
        ? 'Type to queue (sends when current turn finishes)'
        : chatStore.isStreaming
          ? 'Type to queue (sends at the next sub-turn halt)'
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
    position: relative;
    padding: 18px var(--spacing-md);
    background: var(--bg-base);
  }

  .input-area::before {
    content: '';
    position: absolute;
    inset: 0;
    background: var(--bg-elevated);
    border-top: 1px solid var(--border-subtle);
    transform: translateY(100%);
    transition: transform 250ms cubic-bezier(0.4, 0, 0.2, 1);
    pointer-events: none;
    z-index: 0;
  }

  .input-area.both-open::before {
    transform: translateY(0);
  }

  .input-area > :global(*) {
    position: relative;
    z-index: 1;
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
