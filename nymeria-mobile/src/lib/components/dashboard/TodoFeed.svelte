<script lang="ts">
  import { todosStore } from '$lib/stores/todos.svelte';
  import TodoItem from './TodoItem.svelte';
  import TodoForm from './TodoForm.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
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

  let showCompleted = $state(false);
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
    <div class="feed-state">
      <span>Loading...</span>
    </div>
  {:else if todosStore.error}
    <div class="feed-state error">
      <p>{todosStore.error}</p>
    </div>
  {:else if todosStore.todos.length === 0}
    <div class="feed-state">
      {#if threadId}
        <p>No tasks in this thread</p>
        <span class="hint">Tasks created in this conversation will appear here</span>
      {:else}
        <p>No tasks yet</p>
        <span class="hint">Tap "Add Task" to create one or let Nymeria add tasks as she works</span>
      {/if}
    </div>
  {:else}
    <!-- In Progress -->
    {#if organized.inProgress.length > 0}
      <div class="todo-group">
        <h3 class="group-label">
          <span class="label-text">In Progress</span>
          <span class="count highlight">{organized.inProgress.length}</span>
        </h3>
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
    {/if}

    <!-- Upcoming (pending) -->
    {#if organized.active.length > 0}
      <div class="todo-group">
        <h3 class="group-label">
          <span class="label-text">Upcoming</span>
          <span class="count">{organized.active.length}</span>
        </h3>
        {#each organized.active as todo (todo.id)}
          <TodoItem
            {todo}
            onEdit={openEditForm}
            threadTitle={threadTitleMap && todo.threadId ? threadTitleMap[todo.threadId] : undefined}
            onNavigateToThread={onNavigateToThread && todo.threadId ? () => onNavigateToThread!(todo.threadId!) : undefined}
          />
        {/each}
      </div>
    {/if}

    <!-- Completed toggle -->
    {#if organized.completed.length > 0}
      <button class="completed-toggle" onclick={() => (showCompleted = !showCompleted)}>
        <span>{showCompleted ? 'Hide' : 'Show'} completed ({organized.completed.length})</span>
      </button>

      {#if showCompleted}
        {#each organized.completed as todo (todo.id)}
          <TodoItem
            {todo}
            onEdit={openEditForm}
            threadTitle={threadTitleMap && todo.threadId ? threadTitleMap[todo.threadId] : undefined}
            onNavigateToThread={onNavigateToThread && todo.threadId ? () => onNavigateToThread!(todo.threadId!) : undefined}
          />
        {/each}
      {/if}
    {/if}
  {/if}
</div>

<!-- Todo Form Modal -->
<TodoForm isOpen={showForm} onClose={closeForm} editTodo={editingTodo} />

<style>
  .todo-feed {
    display: flex;
    flex-direction: column;
  }

  .feed-header {
    display: flex;
    justify-content: flex-end;
    padding: var(--spacing-sm) var(--spacing-md);
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
    min-height: 36px;
  }

  .add-task-btn:active {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    background: var(--accent-primary-alpha);
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

  .todo-group {
    display: flex;
    flex-direction: column;
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0;
    padding: var(--spacing-xs) var(--spacing-md);
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
    background: var(--bg-elevated-2, var(--bg-hover));
    border-radius: 9px;
  }

  .count.highlight {
    background: var(--accent-primary);
    color: var(--bg-base);
  }

  .completed-toggle {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    text-align: center;
  }

  .completed-toggle:active {
    background: var(--bg-hover);
  }
</style>
