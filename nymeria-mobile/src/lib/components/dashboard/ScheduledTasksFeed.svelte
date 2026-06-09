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
    <div class="feed-state">
      <span>Loading scheduled tasks…</span>
    </div>
  {:else if todosStore.error}
    <div class="feed-state error">
      <p>{todosStore.error}</p>
    </div>
  {:else if !hasScheduledItems}
    <div class="feed-state">
      <p>No scheduled tasks</p>
      <span class="hint">Scheduled TODOs will wake up Nymeria</span>
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

  .feed-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    font-size: var(--font-size-sm);
  }

  .feed-state.error {
    color: var(--error);
  }

  .feed-state p {
    margin: 0;
  }

  .hint {
    font-size: var(--font-size-xs);
    display: block;
    margin-top: var(--spacing-xs);
  }

  .tasks-list {
    display: flex;
    flex-direction: column;
  }
</style>
