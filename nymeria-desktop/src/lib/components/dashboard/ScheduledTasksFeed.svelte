<script lang="ts">
  import { todosStore } from '$lib/stores/todos.svelte';
  import ScheduledTodoItem from './ScheduledTodoItem.svelte';
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
      <span class="loading-text">Loading...</span>
    </div>
  {:else if todosStore.error}
    <div class="error-state">
      <p>{todosStore.error}</p>
    </div>
  {:else if !hasScheduledItems}
    <div class="empty-state">
      <p>No scheduled tasks</p>
      <p class="hint">Add scheduled_for to TODOs to wake up Nymeria</p>
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

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: var(--error);
  }

  .loading-text {
    font-size: var(--font-size-sm);
  }

  .tasks-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }
</style>
