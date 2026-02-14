<script lang="ts">
  import { todosStore } from '$lib/stores/todos.svelte';
  import TodoItem from './TodoItem.svelte';
  import TodoForm from './TodoForm.svelte';
  import { Icon } from '$lib/components/common';
  import { onMount } from 'svelte';
  import type { TodoItem as TodoItemType } from '$lib/types';

  interface Props {
    threadId?: string;
    threadTitleMap?: Record<string, string>;
    onNavigateToThread?: (threadId: string) => void;
  }

  let { threadId, threadTitleMap, onNavigateToThread }: Props = $props();

  onMount(() => {
    todosStore.fetch(undefined, threadId);
  });

  // Re-fetch when threadId changes
  $effect(() => {
    todosStore.fetch(undefined, threadId);
  });

  let organized = $derived(todosStore.organizedTodos);

  // Modal state
  let showForm = $state(false);
  let editingTodo = $state<TodoItemType | null>(null);

  function openCreateForm() {
    editingTodo = null;
    showForm = true;
  }

  function openEditForm(todo: TodoItemType) {
    editingTodo = todo;
    showForm = true;
  }

  function closeForm() {
    showForm = false;
    editingTodo = null;
  }
</script>

<div class="todo-feed">
  <!-- Header with Add Button -->
  <div class="feed-header">
    <button class="add-task-btn" onclick={openCreateForm} type="button">
      <Icon name="plus" size={14} />
      <span>Add Task</span>
    </button>
  </div>

  {#if todosStore.loading && todosStore.todos.length === 0}
    <div class="loading-state">
      <span class="loading-text">Loading...</span>
    </div>
  {:else if todosStore.error}
    <div class="error-state">
      <p>{todosStore.error}</p>
      <button class="retry-btn" onclick={() => todosStore.fetch()}>
        Retry
      </button>
    </div>
  {:else if todosStore.todos.length === 0}
    <div class="empty-state">
      {#if threadId}
        <p>No tasks in this thread</p>
        <p class="hint">Tasks created in this conversation will appear here</p>
      {:else}
        <p>No tasks yet</p>
        <p class="hint">Click "Add Task" to create one or let Nymeria add tasks as she works</p>
      {/if}
    </div>
  {:else}
    <!-- In Progress Section (highlighted at top) -->
    {#if organized.inProgress.length > 0}
      <div class="todo-group in-progress-group">
        <h3 class="group-label">
          <span class="label-text">In Progress</span>
          <span class="count highlight">{organized.inProgress.length}</span>
        </h3>
        <div class="group-items highlighted">
          {#each organized.inProgress as todo (todo.id)}
            <TodoItem
              {todo}
              highlighted={true}
              onEdit={openEditForm}
              threadTitle={threadTitleMap && todo.threadId ? threadTitleMap[todo.threadId] : undefined}
              onNavigateToThread={onNavigateToThread && todo.threadId ? () => onNavigateToThread!(todo.threadId!) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}

    <!-- Active Tasks Section (pending/blocked) -->
    {#if organized.active.length > 0}
      <div class="todo-group">
        <h3 class="group-label">
          <span class="label-text">Upcoming</span>
          <span class="count">{organized.active.length}</span>
        </h3>
        <div class="group-items">
          {#each organized.active as todo (todo.id)}
            <TodoItem
              {todo}
              onEdit={openEditForm}
              threadTitle={threadTitleMap && todo.threadId ? threadTitleMap[todo.threadId] : undefined}
              onNavigateToThread={onNavigateToThread && todo.threadId ? () => onNavigateToThread!(todo.threadId!) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}

    <!-- Completed Section (scrollable like Activity) -->
    {#if organized.completed.length > 0}
      <div class="todo-group completed-group">
        <h3 class="group-label">
          <span class="label-text">Completed</span>
          <span class="count">{organized.completed.length}</span>
        </h3>
        <div class="group-items completed-items">
          {#each organized.completed as todo (todo.id)}
            <TodoItem
              {todo}
              onEdit={openEditForm}
              threadTitle={threadTitleMap && todo.threadId ? threadTitleMap[todo.threadId] : undefined}
              onNavigateToThread={onNavigateToThread && todo.threadId ? () => onNavigateToThread!(todo.threadId!) : undefined}
            />
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<!-- Todo Form Modal -->
<TodoForm isOpen={showForm} onClose={closeForm} editTodo={editingTodo} />

<style>
  .todo-feed {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .feed-header {
    display: flex;
    justify-content: flex-end;
  }

  .add-task-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .add-task-btn:hover {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.05);
  }

  .loading-state,
  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-sm);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: var(--error);
  }

  .error-state p {
    margin: 0 0 var(--spacing-sm);
  }

  .retry-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    background: transparent;
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .retry-btn:hover {
    background: var(--error);
    color: var(--bg-base);
  }

  .loading-text {
    font-size: var(--font-size-sm);
  }

  .todo-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-full);
  }

  .count.highlight {
    background: var(--accent-primary);
    color: var(--bg-base);
  }

  .group-items {
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .group-items.highlighted {
    background: rgba(var(--accent-primary-rgb), 0.1);
    border: 1px solid rgba(var(--accent-primary-rgb), 0.3);
  }

  /* Completed section scrollable like Activity feed */
  .completed-group .completed-items {
    max-height: 300px;
    overflow-y: auto;
  }
</style>
