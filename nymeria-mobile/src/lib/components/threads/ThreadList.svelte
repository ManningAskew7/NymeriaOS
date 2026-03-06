<script lang="ts">
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import ThreadItem from './ThreadItem.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  let searchQuery = $state('');

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
          <p>No conversations yet</p>
          <span>Start a new chat to begin</span>
        {/if}
      </div>
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
    min-height: 36px;
  }

  .search-bar input:focus {
    outline: none;
    box-shadow: none;
  }

  .groups {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
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
</style>
