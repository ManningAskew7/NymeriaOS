<script lang="ts">
  import { todosStore } from '$lib/stores/todos.svelte';
  import ScheduledTodoItem from './ScheduledTodoItem.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';
  import { onMount } from 'svelte';

  onMount(() => {
    todosStore.fetch();
  });

  let hasScheduledItems = $derived(todosStore.scheduledCount > 0);
  let isLoading = $derived(todosStore.loading && todosStore.todos.length === 0);
</script>

<div class="tasks-feed">
  {#if isLoading}
    <div class="loading-state">
      <InlineLoader text="Loading scheduled tasks…" />
    </div>
  {:else if todosStore.error}
    <div class="error-state">
      <p>{todosStore.error}</p>
    </div>
  {:else if !hasScheduledItems}
    <div class="empty-state">
      <Icon name="clock" size={24} />
      <p>No scheduled tasks</p>
      <p class="hint">Schedule a task for later to wake Nymeria up automatically.</p>
    </div>
  {:else}
    <div class="tasks-list">
      {#each todosStore.scheduledTodos as todo (todo.id)}
        <ScheduledTodoItem {todo} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .tasks-feed {
    display: flex;
    flex-direction: column;
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-md);
  }

  /* Loading text sits one size down (pairs with the InlineLoader's sm
     spinner); this lived on the removed .loading-text span before. */
  .loading-state {
    font-size: var(--font-size-sm);
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-xs);
  }

  .error-state {
    color: var(--error);
  }

  .tasks-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }
</style>
