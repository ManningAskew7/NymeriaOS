<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import { ChatContainer, InputBar, ContextStatusBar } from '$lib/components/chat';
  import { ThreadSettingsPanel } from '$lib/components/threads';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import type { FileAttachment, SSEEvent } from '$lib/types';

  let currentTitle = $derived(threadsStore.currentThread?.title ?? 'New Chat');
  let showThreadSettings = $state(false);

  async function handleSend(message: string, attachments?: FileAttachment[]) {
    if (!message.trim() && (!attachments || attachments.length === 0)) return;

    // Create thread if needed
    if (!threadsStore.currentThreadId) {
      const thread = threadsStore.createThread();
      threadsStore.selectThread(thread.id);
    }

    const threadId = threadsStore.currentThreadId!;

    // Auto-title from first message
    threadsStore.autoTitleFromMessage(threadId, message);

    // Add user message to chat
    chatStore.addUserMessage(message, attachments);
    chatStore.addAssistantMessage();
    chatStore.setStreaming(true);

    try {
      for await (const event of api.chatStream(message, threadId, attachments)) {
        handleSSEEvent(event, threadId);
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        // User cancelled
      } else {
        chatStore.setLastMessageError(
          error instanceof Error ? error.message : 'Connection lost'
        );
      }
    } finally {
      chatStore.reclassifyThinkingAsResponse();
      chatStore.setLastMessageComplete();
      chatStore.setStreaming(false);
      chatStore.clearActiveToolCalls();
    }
  }

  function handleSSEEvent(event: SSEEvent, threadId: string) {
    // Stale event guard
    if (threadsStore.currentThreadId !== threadId) return;

    switch (event.type) {
      case 'thinking':
        chatStore.addThinkingStep((event.data as { message: string }).message);
        break;

      case 'response':
        chatStore.addResponseStep((event.data as { content: string }).content);
        break;

      case 'tool_call': {
        const tc = event.data as { id: string; name: string; arguments: Record<string, unknown> };
        chatStore.addToolCallStep(tc.id, tc.name, tc.arguments);
        break;
      }

      case 'tool_result': {
        const tr = event.data as { id?: string; name: string; result: string; status: string };
        if (tr.id) {
          chatStore.updateToolCallStepResult(tr.id, tr.result, tr.status === 'error' ? 'error' : 'success');
        } else {
          chatStore.updateToolCallResultByName(tr.name, tr.result, tr.status === 'error' ? 'error' : 'success');
        }
        break;
      }

      case 'error':
        chatStore.setLastMessageError((event.data as { message: string }).message);
        break;

      case 'done': {
        const doneData = event.data as { threadId: string; contextStats?: unknown; model?: string };
        if (doneData.contextStats) {
          chatStore.setContextStats(doneData.contextStats as import('$lib/types').ContextStats);
        }
        if (doneData.model) {
          chatStore.setActiveModel(doneData.model);
        }
        // Ensure thread exists in sidebar
        if (event.threadId) {
          threadsStore.setThreadFromApi(event.threadId, currentTitle);
        }
        break;
      }

      case 'queued':
        chatStore.setQueued(true);
        break;

      case 'compacting':
        chatStore.setCompacting(true, (event.data as { message: string }).message);
        break;

      case 'compact_result':
        chatStore.setCompactResult((event.data as { messagesRemoved: number }).messagesRemoved);
        break;

      case 'compacted': {
        const cd = event.data as { messagesRemoved: number; summary?: string };
        chatStore.handleCompacted(cd.messagesRemoved, cd.summary);
        break;
      }

      case 'context_attached':
        chatStore.setContextAttached((event.data as { summary: string }).summary);
        chatStore.setLastUserMessageContextSummary((event.data as { summary: string }).summary);
        break;

      case 'iteration_limit':
        chatStore.setLastMessageError((event.data as { message: string }).message);
        break;
    }
  }
</script>

<div class="chat-panel">
  <!-- Thread header -->
  <div class="chat-header">
    <button
      class="header-btn"
      onclick={() => uiStore.goToPanel('left')}
      title="Threads"
    >
      <Icon name="menu" size={22} />
    </button>

    <div class="header-title">
      <span class="title-text">{currentTitle}</span>
    </div>

    <div class="header-actions">
      {#if threadsStore.currentThreadId}
        <button
          class="header-btn"
          onclick={() => (showThreadSettings = true)}
          title="Thread Settings"
        >
          <Icon name="settings" size={20} />
        </button>
      {/if}
      <button
        class="header-btn"
        onclick={() => uiStore.goToPanel('right')}
        title="Dashboard"
      >
        <Icon name="bolt" size={22} />
      </button>
    </div>
  </div>

  <!-- Messages area -->
  <div class="messages-area">
    <ChatContainer />
  </div>

  <!-- Context stats (model, tokens, usage) -->
  <ContextStatusBar />

  <!-- Input bar -->
  <InputBar
    onSend={handleSend}
    disabled={!configStore.isConfigured}
    filesEnabled={configStore.isConfigured}
  />
</div>

{#if threadsStore.currentThreadId}
  <ThreadSettingsPanel
    open={showThreadSettings}
    threadId={threadsStore.currentThreadId}
    onClose={() => (showThreadSettings = false)}
  />
{/if}

<style>
  .chat-panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-base);
  }

  .chat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 var(--spacing-sm);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    flex-shrink: 0;
  }

  .header-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    transition: all var(--transition-fast);
  }

  .header-btn:active {
    background: var(--bg-hover);
    color: var(--accent-primary);
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0;
  }

  .header-title {
    flex: 1;
    text-align: center;
    overflow: hidden;
    min-width: 0;
  }

  .title-text {
    display: block;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .messages-area {
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
</style>
