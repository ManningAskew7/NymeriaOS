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

  <!-- Wrap context bar + input together so the elevated background slides
       in/out as a single unit when sidebars collapse, instead of leaving
       the context bar visually orphaned with its own elevated colour. -->
  <div class="input-section" class:both-open={bothSidebarsOpen}>
    <ContextStatusBar />
    <div class="input-area">
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
    position: relative;
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

  /* Scroll padding lives INSIDE ChatContainer's .chat-container so the chat
     scroll content can extend behind the absolutely-positioned input-section.
     Reserving the room here (instead of as padding on .chat-area) keeps
     chat-area's box flush with the bottom of main-panel-content — when the
     input-section's ::before sheet slides away on sidebar collapse, the
     newly transparent section reveals chat scroll content underneath
     instead of an empty bg-base slab. */
  .chat-area :global(.chat-container) {
    /* Must clear the absolutely-positioned input-section's full natural
       height: 20px bar + 14px top padding + ~50px pill + 14px bottom
       padding + ~24px hint + ~10px safety = ~140px. Lower values let the
       tail of the chat scroll under the bar/pill area, hiding messages. */
    padding-bottom: 140px;
  }

  /* Wrapper for ContextStatusBar + InputBar. Owns the sliding elevated
     background so both halves move together when a sidebar collapses.
     The section itself stays TRANSPARENT — the prompt-window surface is
     painted by ::before (starts at top: 20px), and the bar surface is
     painted by ContextStatusBar's .bar-content. When the bar is collapsed,
     the 20px above the prompt window must read-through to the chat area
     behind it, so this wrapper must NOT set its own background. */
  .input-section {
    /* Absolutely positioned over the bottom of the chat area so that when
       ::before slides away on sidebar collapse, the now-transparent section
       reveals chat content underneath — instead of main-panel-content's
       bg-base, which would read as a dark slab. */
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    background: transparent;
  }

  /* Two sliding sheets, both sized to the full section (inset: 0) so a
     single translateY(100%) actually clears the section. Each pseudo
     paints only the portion of bg it owns via a clipped linear-gradient;
     the rest of the pseudo is transparent.

     ::before = BAR  (paints top 20px + has its own border-top divider at
                      y=0). Earlier in source order so it renders UNDER
                      ::after — meaning the bar slides BEHIND the prompt
                      window, hidden by ::after's opaque bg during the
                      slide instead of passing in front of it. Slides on
                      local chevron collapse (via :has()) AND on sidebar
                      collapse.
     ::after  = PROMPT WINDOW (paints from y=20 down + has a 1px under-bar
                      divider line at y=20-21). Later in source order so
                      it renders ON TOP, providing the opaque mask that
                      hides ::before's slide. Slides on sidebar collapse
                      only.

     Because both pseudos are the same height and translate by the same
     distance (= section_height) on sidebar collapse, they move together
     at a single velocity — unified slide, not a desync.

     Each bg stacks a glass-bg-strong tint over a solid bg-base so the
     result is fully opaque (no chat bleed-through when both sidebars
     are open). */
  /* ::before = BAR (renders under ::after) */
  .input-section::before {
    content: '';
    position: absolute;
    inset: 0;
    background:
      linear-gradient(to bottom, var(--glass-bg-strong) 0 20px, transparent 20px),
      linear-gradient(to bottom, var(--bg-base) 0 20px, transparent 20px);
    /* Bar's top divider painted via inset box-shadow at the top edge. */
    box-shadow: inset 0 1px 0 var(--border-subtle);
    transform: translateY(100%);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    pointer-events: none;
    z-index: 0;
  }

  /* ::after = PROMPT WINDOW (renders on top, masking the bar's slide) */
  .input-section::after {
    content: '';
    position: absolute;
    inset: 0;
    background:
      linear-gradient(
        to bottom,
        transparent 0 20px,
        var(--border-subtle) 20px 21px,
        var(--glass-bg-strong) 21px
      ),
      linear-gradient(to bottom, transparent 0 20px, var(--bg-base) 20px);
    transform: translateY(100%);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    pointer-events: none;
    z-index: 0;
  }

  .input-section.both-open::after {
    transform: translateY(0);
  }

  /* ::before (bar bg) at rest only when sidebars are both open AND the bar
     isn't locally collapsed. Chevron click toggles
     .context-status-bar.collapsed which trips the :has() check, dropping
     ::before back to translateY(100%) so the bar's bg + top divider slide
     down behind ::after (the prompt window) alongside the text. */
  .input-section.both-open:not(:has(.context-status-bar.collapsed))::before {
    transform: translateY(0);
  }

  .input-section > :global(*) {
    position: relative;
    z-index: 1;
  }

  .input-area {
    /* Single source of truth for the three vertical gaps that flank the
       prompt input bar:
         1. above the input-container  (padding-top of .input-area)
         2. between input-container and the "Press Ctrl+Enter…" hint
            (the hint's margin-top, overridden via :global below)
         3. below the hint  (padding-bottom of .input-area)
       Changing --prompt-stack-gap below resizes ALL THREE gaps in lock step
       so they remain equal even if the input or hint heights change later.
       To break this equality intentionally, override the individual values
       on .input-area or the :global(.hint) rule. */
    --prompt-stack-gap: 14px;
    padding: var(--prompt-stack-gap) var(--spacing-md);
    /* The InputBar component handles its own internal padding. The divider
       between context bar and input is drawn by the sliding pseudo in
       .input-section::before so it moves with the bg. */
  }

  /* Override InputBar's default 18px hint margin-top so it stays locked to
     the same gap value as the .input-area paddings above and below. */
  .input-area :global(.hint) {
    margin-top: var(--prompt-stack-gap);
  }

  /* When a sidebar collapses, the "Press Ctrl+Enter…" hint that lives at the
     bottom of the InputBar slides down + collapses out of view. Two effects
     combine: (1) transform translates it down so it visually slides off the
     elevated surface, (2) max-height + margin-top + opacity go to 0 so the
     space it occupied also shrinks — which pulls the prompt input bar lower
     on the screen (the .input-section is bottom-anchored in MainPanel's
     flex column, so a shorter section means the top of the bar drops). */
  .input-section :global(.hint) {
    max-height: 48px;
    overflow: hidden;
    transition:
      max-height var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      margin-top var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section:not(.both-open) :global(.hint) {
    max-height: 0;
    margin-top: 0;
    opacity: 0;
    transform: translateY(20px);
    pointer-events: none;
  }

  /* Sidebar-closed bar drop-down: when a sidebar is closed, the bg sheets
     have slid away, leaving the bar's text + dot orphaned at the top of an
     otherwise transparent section. Translate the whole .context-status-bar
     down so its contents sit just above the prompt input field instead of
     floating up where the bar used to live. The whole bar element moves as
     one unit, so the bar's own overflow:hidden clipping moves with it — no
     content gets cut off, and the text+dot stay in their normal relative
     positions inside the (now relocated) bar. */
  .input-section :global(.context-status-bar) {
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
    /* Promote above the input-area (both default to z-index:1 from the
       global `.input-section > *` rule, and source order would put
       input-area on top — swallowing dot clicks once the sidebar-closed
       translateY(16px) below pushes the bar's footprint over the prompt
       input). Bumping to 2 keeps the dot reachable in every state. */
    z-index: 2;
  }
  .input-section:not(.both-open) :global(.context-status-bar) {
    transform: translateY(16px);
  }

  /* Sidebar collapse: only the ::before / ::after sheets slide. The bar
     text and chevron stay put and fully functional — the chevron can
     still toggle the bar text via the bar's own .collapsed state even
     when both sidebars are closed.

     SIDEBAR-CLOSED CHEVRON ANIMATION:
     When sidebars are CLOSED and the chevron is clicked to uncollapse
     the bar, the text slides in left-to-right via a clip-path wipe
     (collapse goes the other way, clipping right-to-left). This swaps
     out the bar's normal translateY+opacity slide ONLY in the closed
     state — when sidebars are open, the bar text still uses its
     internal vertical slide. */
  /* Default visible clip-path lives on a rule that's ALWAYS active (no
     :not() gate) — otherwise the transition has nothing to interpolate
     from (clip-path defaults to `none`, which can't animate to `inset()`).
     The transition declaration lives here too so it remains in effect
     regardless of sidebar state. */
  .input-section :global(.context-status-bar .bar-content) {
    visibility: visible;
    clip-path: inset(0 0 0 0);
    /* Must list transform + opacity here too, not just clip-path —
       this :global rule's selector is more specific than the bar's
       internal .bar-content rule, so it replaces (not augments) the
       transition shorthand. Dropping transform/opacity would kill the
       sidebar-open chevron's vertical slide.
       Visibility flips to visible INSTANTLY on this rule (0s/0s delay) so
       chevron-uncollapse reveals the text right at the start of the
       slide-up / clip-path wipe — no fade-in delay needed. */
    transition:
      visibility 0s linear 0s,
      clip-path 360ms cubic-bezier(0.22, 1, 0.36, 1),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  /* Whenever the bar is collapsed — regardless of sidebar state — the text
     must read as invisible. Visibility flips to hidden AFTER the slide-down
     completes (delay = sidebar-collapse-duration) so the chevron-collapse
     animation still plays out, but once hidden, sidebar transitions can't
     re-show it. This is what kills the "text flashes during sidebar
     collapse" bug: opacity/transform/clip-path are free to animate to their
     new staged values behind a visibility:hidden mask, so the user never
     sees the in-between frames. */
  .input-section :global(.context-status-bar.collapsed .bar-content) {
    visibility: hidden;
    transition:
      visibility 0s linear var(--sidebar-collapse-duration),
      clip-path 360ms cubic-bezier(0.22, 1, 0.36, 1),
      transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
      opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section:not(.both-open) :global(.context-status-bar.collapsed .bar-content) {
    /* Cancel the bar's internal translateY/opacity so only the clip-path
       wipe is visible when collapsing with sidebars closed. */
    transform: translateY(0);
    opacity: 1;
    clip-path: inset(0 100% 0 0);
  }

  /* When sidebars are collapsed, the bar sits without its elevated bg
     behind it — the text reads slightly low against the transparent chat
     area. Nudge the inner .details container up 2px so it optically
     centers on the bar's vertical mid-line in this state. */
  .input-section:not(.both-open) :global(.context-status-bar .details) {
    transform: translateY(-2px);
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }
  .input-section :global(.context-status-bar .details) {
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
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
