<script lang="ts">
  import { todosStore } from '$lib/stores/todos.svelte';
  import TodoItem from './TodoItem.svelte';
  import TodoForm from './TodoForm.svelte';
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
  <!-- No loading-state branch on purpose. When this feed is inside a
       collapsible section, the Svelte slide transition measures the .content
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
      <button type="button" class="retry-btn" onclick={() => todosStore.fetch()}>
        Retry
      </button>
    </div>
  {:else if todosStore.todos.length === 0}
    <div class="empty-state">
      {#if threadId}
        <p>No tasks in this thread</p>
        <p class="hint">Use "New task" in the section header to create one for this thread</p>
      {:else}
        <p>No tasks yet</p>
        <p class="hint">Click "New task" in the section header to create one. Nymeria can also create tasks while working on a thread.</p>
      {/if}
    </div>
  {:else}
    <!-- In Progress Section (highlighted at top) -->
    {#if organized.inProgress.length > 0}
      <div class="todo-group in-progress-group">
        <h3 class="group-label section-label">
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
        <h3 class="group-label section-label">
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
        <h3 class="group-label section-label">
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
    /* Count sits right after the label text (spaced by the gap), reading as
       "Upcoming 2" rather than floating at the row's right edge. */
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
    /* Type role (uppercase / tracking / weight / muted) is the global .section-label. */
    /* Indent to the 16px icon column (8px section body + 8px here) so the group
       label shares the chevron / row-icon vertical line. */
    padding-left: var(--spacing-sm);
  }

  /* Reserve the shared count-column width so the count begins at the same x as
     every other count in the dashboard panel. Falls back to 0 (count sits right
     after the label) wherever --count-col-label is not defined. */
  .label-text {
    min-width: var(--count-col-label, 0px);
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
    /* Rows sit flush (no gap): a hairline divider on each row but the first
       (see TodoItem) provides the separation, so the list reads as one
       continuous group inside the section card rather than a stack of cards. */
    gap: 0;
  }

  /* Completed section scrollable like Activity feed */
  .completed-group .completed-items {
    max-height: 300px;
    overflow-y: auto;
  }
</style>
