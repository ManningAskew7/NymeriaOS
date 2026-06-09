<script lang="ts">
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import ThreadItem from './ThreadItem.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  let searchQuery = $state('');
  let retryingSync = $state(false);

  let filteredGroups = $derived.by(() => {
    const groups = threadsStore.groupedUnfiledThreads;
    if (!searchQuery.trim()) return groups;

    const query = searchQuery.toLowerCase();
    return groups
      .map((g) => ({
        ...g,
        threads: g.threads.filter((t) =>
          t.title.toLowerCase().includes(query)
        ),
      }))
      .filter((g) => g.threads.length > 0);
  });

  function handleSelect(threadId: string) {
    switchToThread(threadId);
  }

  function handleDelete(threadId: string) {
    if (confirm('Delete this thread?')) {
      threadsStore.deleteThread(threadId);
    }
  }

  async function handleRetrySync() {
    retryingSync = true;
    try {
      await threadsStore.syncFromBackend();
    } finally {
      retryingSync = false;
    }
  }
</script>

<div class="thread-list">
  <!-- Search -->
  <div class="search-bar">
    <Icon name="chat" size={16} />
    <input
      type="text"
      placeholder="Search threads..."
      bind:value={searchQuery}
    />
  </div>

  <!-- Thread groups -->
  <div class="groups">
    {#if !threadsStore.initialSyncDone}
      <div class="sync-loading">
        <Icon name="loading" size={20} />
        <span>Loading threads…</span>
      </div>
    {:else}
      {#if threadsStore.lastSyncError}
        <div class="sync-error-banner">
          <p>Couldn't sync threads from backend. Showing cached data.</p>
          <button type="button" disabled={retryingSync} onclick={handleRetrySync}>
            {retryingSync ? 'Retrying…' : 'Retry'}
          </button>
        </div>
      {/if}

      {#each filteredGroups as group}
        <div class="group">
          <div class="group-label">{group.label}</div>
          {#each group.threads as thread (thread.id)}
            <ThreadItem
              {thread}
              isActive={thread.id === threadsStore.currentThreadId}
              onSelect={handleSelect}
              onDelete={handleDelete}
            />
          {/each}
        </div>
      {/each}

      {#if filteredGroups.length === 0}
        <div class="empty">
          {#if searchQuery}
            <p>No threads match "{searchQuery}"</p>
          {:else}
            <Icon name="chat" size={32} />
            <p>No threads yet</p>
            <span>Start a new thread to begin</span>
          {/if}
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  .thread-list {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .search-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    color: var(--text-muted);
  }

  .search-bar input {
    flex: 1;
    border: none;
    background: transparent;
    font-size: 16px;
    color: var(--text-primary);
    padding: var(--spacing-sm) 0;
    min-height: var(--touch-target-min);
  }

  .search-bar input:focus {
    outline: none;
    box-shadow: none;
  }

  .groups {
    flex: 1;
    overflow-y: auto;
  }

  .group-label {
    padding: var(--spacing-sm) var(--spacing-lg);
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    background: var(--bg-elevated);
    position: sticky;
    top: 0;
    z-index: 1;
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xl);
    gap: var(--spacing-sm);
    color: var(--text-muted);
    text-align: center;
  }

  .empty p {
    font-size: var(--font-size-base);
    color: var(--text-secondary);
  }

  .empty span {
    font-size: var(--font-size-sm);
  }

  .sync-loading {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xl);
    color: var(--text-muted);
    font-size: var(--font-size-base);
  }

  .sync-error-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    margin: var(--spacing-sm);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .sync-error-banner p {
    margin: 0;
    flex: 1;
  }

  .sync-error-banner button {
    min-height: var(--touch-target-min);
    padding: 0 var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    background: transparent;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .sync-error-banner button:disabled {
    opacity: 0.6;
  }
</style>
