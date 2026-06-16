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
    <div class="empty-state">
      <InlineLoader text="Loading scheduled tasks…" />
    </div>
  {:else if todosStore.error}
    <div class="empty-state error">
      <p>{todosStore.error}</p>
    </div>
  {:else if !hasScheduledItems}
    <div class="empty-state">
      <Icon name="clock" size={24} />
      <p>No scheduled tasks</p>
      <span class="hint">Schedule a task for later to wake Nymeria up automatically.</span>
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

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    font-size: var(--font-size-sm);
  }

  .empty-state.error {
    color: var(--error);
  }

  .empty-state p {
    margin: 0;
  }

  .hint {
    font-size: var(--font-size-xs);
    display: block;
  }

  .tasks-list {
    display: flex;
    flex-direction: column;
  }
</style>
