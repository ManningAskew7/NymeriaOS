<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import { ChatContainer, InputBar, ContextStatusBar } from '$lib/components/chat';
  import { ThreadSettingsPanel } from '$lib/components/threads';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { untrack } from 'svelte';
  import type { FileAttachment, SSEEvent } from '$lib/types';

  let currentTitle = $derived(threadsStore.currentThread?.title ?? 'New Chat');
  let showThreadSettings = $state(false);

  // Load global stores for header badges
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
    if (tid) {
      untrack(() => threadConfigStore.loadConfig(tid));
    }
  });

  const currentThreadConfig = $derived(
    threadsStore.currentThreadId
      ? threadConfigStore.getConfig(threadsStore.currentThreadId) ?? null
      : null
  );

  function shortModelName(modelId: string): string {
    const parts = modelId.split('/');
    return parts[parts.length - 1];
  }

  const effectiveModel = $derived.by(() => {
    if (currentThreadConfig?.llmConfig?.model) {
      return { name: shortModelName(currentThreadConfig.llmConfig.model), isOverride: true };
    }
    if (serverSettingsStore.model) {
      return { name: shortModelName(serverSettingsStore.model), isOverride: false };
    }
    return null;
  });

  const disabledCount = $derived(currentThreadConfig?.disabledTools?.length ?? 0);
  const enabledOptionalCount = $derived(currentThreadConfig?.enabledTools?.length ?? 0);

  const activeToolCount = $derived.by(() => {
    if (!defaultToolsStore.loaded) return null;
    return defaultToolsStore.defaultToolNames.length - disabledCount + enabledOptionalCount;
  });

  const callableCount = $derived(defaultToolsStore.callableThreadCount);

  const triggerCount = $derived(
    threadsStore.currentThreadId
      ? triggersStore.triggers.filter(t => t.enabled && t.thread_id === threadsStore.currentThreadId).length
      : 0
  );

  const hasInstructions = $derived(!!currentThreadConfig?.instructions);
  const isCallable = $derived(currentThreadConfig?.callable ?? false);
  const hasBadges = $derived(
    effectiveModel !== null || activeToolCount !== null || callableCount > 0 ||
    triggerCount > 0 || hasInstructions || isCallable
  );

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
        {
          const data = event.data as {
            message: string;
            reason?: 'max_iterations' | 'repeated_tool_result';
          };
          chatStore.setLastMessageError(
            data.reason === 'repeated_tool_result'
              ? `Repeated tool loop stopped. ${data.message}`
              : data.message
          );
        }
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

    <div class="header-center">
      <div class="header-title">
        <span class="title-text">{currentTitle}</span>
      </div>
      {#if threadsStore.currentThreadId && hasBadges}
        <div class="header-badges">
          {#if effectiveModel}
            <span
              class="badge model-badge"
              class:default={!effectiveModel.isOverride}
              class:override={effectiveModel.isOverride}
            >
              {effectiveModel.name}
            </span>
          {/if}
          {#if activeToolCount !== null}
            <span class="badge tools-badge" class:reduced={disabledCount > 0}>
              {activeToolCount} tools
            </span>
          {/if}
          {#if callableCount > 0}
            <span class="badge callables-badge">
              {callableCount} callable{callableCount !== 1 ? 's' : ''}
            </span>
          {/if}
          {#if triggerCount > 0}
            <span class="badge triggers-badge">
              {triggerCount} trigger{triggerCount !== 1 ? 's' : ''}
            </span>
          {/if}
          {#if hasInstructions}
            <span class="badge instructions-badge">instructions</span>
          {/if}
          {#if isCallable}
            <span class="badge callable-badge">&lt; Callable</span>
          {/if}
        </div>
      {/if}
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

  .header-center {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow: hidden;
    min-width: 0;
    gap: 2px;
  }

  .header-title {
    width: 100%;
    text-align: center;
    overflow: hidden;
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

  .header-badges {
    display: flex;
    align-items: center;
    gap: 3px;
    overflow-x: auto;
    max-width: 100%;
    scrollbar-width: none;
    -ms-overflow-style: none;
  }

  .header-badges::-webkit-scrollbar {
    display: none;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    padding: 0px 4px;
    font-size: 8px;
    font-weight: 500;
    border-radius: var(--radius-full);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .model-badge.override {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .model-badge.default {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .tools-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .tools-badge.reduced {
    background: color-mix(in srgb, var(--warning, #f59e0b) 20%, transparent);
    color: var(--warning, #f59e0b);
    border: 1px solid color-mix(in srgb, var(--warning, #f59e0b) 30%, transparent);
  }

  .callables-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .triggers-badge {
    background: color-mix(in srgb, var(--success, #10b981) 20%, transparent);
    color: var(--success, #10b981);
    border: 1px solid color-mix(in srgb, var(--success, #10b981) 30%, transparent);
  }

  .instructions-badge {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
    border: 1px solid color-mix(in srgb, var(--text-muted) 25%, transparent);
  }

  .callable-badge {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
    color: var(--accent-primary);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .messages-area {
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
</style>
