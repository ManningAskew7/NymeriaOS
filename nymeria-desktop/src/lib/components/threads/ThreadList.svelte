<script lang="ts">
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { api } from '$lib/services/api.svelte';
  import ThreadItem from './ThreadItem.svelte';
  import ThreadSettingsPanel from './ThreadSettingsPanel.svelte';
  import type { Thread, ThreadConfig } from '$lib/types';

  let loadError = $state<string | null>(null);
  let configureThread = $state<Thread | null>(null);

  async function handleSelectThread(threadId: string) {
    if (threadId === threadsStore.currentThreadId) return;

    loadError = null;
    threadsStore.selectThread(threadId);
    chatStore.clearMessages();

    // Load thread history and context stats from API
    try {
      const [history, stats] = await Promise.all([
        api.getThreadHistory(threadId),
        api.getThreadContextStats(threadId),
      ]);
      chatStore.setMessages(history.messages);
      chatStore.setContextStats(stats);
    } catch (error) {
      console.error('Failed to load thread history:', error);
      // Show error but keep the thread selected (might be local-only or corrupted)
      loadError = 'Could not load chat history. The thread may have been created before syncing was fixed.';
      // Optionally delete the broken thread
      // threadsStore.deleteThread(threadId);
    }
  }

  function handleDeleteThread(threadId: string) {
    if (confirm('Are you sure you want to delete this thread?')) {
      threadsStore.deleteThread(threadId);
      if (threadsStore.currentThreadId === threadId) {
        chatStore.clearMessages();
      }
    }
  }

  function handleRenameThread(threadId: string, newTitle: string) {
    threadsStore.renameThread(threadId, newTitle);
  }

  function handleConfigureThread(thread: Thread) {
    // Load config if not cached
    threadConfigStore.loadConfig(thread.id);
    configureThread = thread;
  }

  function handleConfigSaved(config: ThreadConfig) {
    // Config is already in the store
  }
</script>

<div class="thread-list">
  {#if loadError}
    <div class="load-error">
      <p>{loadError}</p>
      <button onclick={() => loadError = null}>Dismiss</button>
    </div>
  {/if}
  {#if threadsStore.groupedThreads.length === 0}
    <div class="empty-state">
      <p>No conversations yet</p>
      <p class="hint">Start a new chat to begin</p>
    </div>
  {:else}
    {#each threadsStore.groupedThreads as group (group.label)}
      <div class="thread-group">
        <h3 class="group-label">{group.label}</h3>
        <div class="group-threads">
          {#each group.threads as thread (thread.id)}
            <ThreadItem
              {thread}
              isActive={thread.id === threadsStore.currentThreadId}
              taskCount={threadsStore.getThreadTaskCount(thread.id)}
              hasActiveTask={threadsStore.isThreadActive(thread.id)}
              hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
              onSelect={() => handleSelectThread(thread.id)}
              onDelete={() => handleDeleteThread(thread.id)}
              onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
              onConfigure={() => handleConfigureThread(thread)}
            />
          {/each}
        </div>
      </div>
    {/each}
  {/if}
</div>

{#if configureThread}
  <ThreadSettingsPanel
    thread={configureThread}
    threadConfig={threadConfigStore.getConfig(configureThread.id) ?? null}
    onClose={() => (configureThread = null)}
    onSaved={handleConfigSaved}
  />
{/if}

<style>
  .thread-list {
    padding: var(--spacing-sm);
  }

  .empty-state {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-sm);
    margin-top: var(--spacing-xs);
  }

  .thread-group {
    margin-bottom: var(--spacing-md);
  }

  .group-label {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: var(--spacing-sm) var(--spacing-md);
    margin: 0;
  }

  .group-threads {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .load-error {
    padding: var(--spacing-sm);
    margin: var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    font-size: var(--font-size-xs);
    color: var(--error);
  }

  .load-error p {
    margin: 0 0 var(--spacing-xs) 0;
  }

  .load-error button {
    font-size: var(--font-size-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px solid var(--error);
    border-radius: var(--radius-sm);
    color: var(--error);
    cursor: pointer;
  }

  .load-error button:hover {
    background: color-mix(in srgb, var(--error) 20%, transparent);
  }
</style>
