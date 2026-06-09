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

  <!-- No loading-state branch on purpose. When this feed is inside a
       Collapsible, the Svelte slide transition measures the .content
       height once at intro-start. If the initial render is the small
       loading-state and the fetch resolves mid-slide, the rendered
       state switches to the (taller) empty-state or todo list, but the
       slide animates to the originally-measured height — the element
       then snaps to its new natural height when the inline transition
       styles clear at the end. Going straight to empty-state on the
       initial render keeps the measured height stable. The brief
       "No tasks" flash before real todos arrive is preferable to the
       snap. -->
  {#if todosStore.error}
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

    <!-- Active Tasks Section (pending) -->
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
    width: 100%;
  }

  .add-task-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .add-task-btn:hover {
    border-color: var(--accent-primary);
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .add-task-btn span {
    transform: translateY(-1px);
  }

  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    /* Dashed border signals "placeholder, not interactive" — distinguishes
       these containers from the Add Task button (solid border) sitting
       directly above them in the panel. */
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-sm);
  }

  .error-state p {
    margin: 0 0 var(--spacing-sm);
  }

  .retry-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--error) 50%, transparent);
    border-radius: var(--radius-md);
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .retry-btn:hover {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border-color: var(--error);
    color: var(--error);
  }

  .todo-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
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
    min-width: 16px;
    height: 16px;
    padding: 0 5px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    background: var(--bg-hover);
    color: var(--text-secondary);
    border-radius: var(--radius-full);
  }

  .count.highlight {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .group-items {
    display: flex;
    flex-direction: column;
    /* Gap between cards uses --spacing-sm so the breathing matches
       TriggerFeed exactly. The 12px internal card padding (todo-item)
       feels grouped within a card, the 8px between cards reads as
       separation — Gestalt proximity working in our favour. */
    gap: var(--spacing-sm);
  }

  .group-items.highlighted {
    padding: var(--spacing-xs);
    background: rgba(var(--accent-primary-rgb), 0.1);
    border: 1px solid var(--accent-tint-border);
    border-radius: var(--radius-md);
  }

  /* Completed section scrollable like Activity feed */
  .completed-group .completed-items {
    max-height: 300px;
    overflow-y: auto;
  }
</style>
