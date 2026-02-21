<script lang="ts">
  import type { TodoItem as TodoItemType } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { onMount, onDestroy } from 'svelte';

  interface Props {
    todo: TodoItemType;
    highlighted?: boolean;
    onEdit?: (todo: TodoItemType) => void;
    threadTitle?: string;
    onNavigateToThread?: () => void;
  }

  let { todo, highlighted = false, onEdit, threadTitle, onNavigateToThread }: Props = $props();
  let completing = $state(false);

  // Collapsible state - default collapsed, expand to see details
  let expanded = $state(false);

  // Reactive time for countdown - updates every second when scheduled
  let now = $state(new Date());
  let intervalId: ReturnType<typeof setInterval> | null = null;

  // Start/stop timer based on whether todo is scheduled
  $effect(() => {
    if (todo.scheduledFor) {
      // Start interval if not already running
      if (!intervalId) {
        intervalId = setInterval(() => {
          now = new Date();
        }, 1000);
      }
    } else {
      // Clear interval if todo is no longer scheduled
      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
      }
    }
  });

  onDestroy(() => {
    if (intervalId) {
      clearInterval(intervalId);
    }
  });

  let statusIcon = $derived.by(() => {
    switch (todo.status) {
      case 'done':
        return 'check';
      case 'in_progress':
        return 'clock';
      default:
        return 'clock';
    }
  });

  let statusColor = $derived.by(() => {
    switch (todo.status) {
      case 'done':
        return 'var(--success)';
      case 'in_progress':
        return 'var(--accent-primary)';
      default:
        return 'var(--text-muted)';
    }
  });

  // Format scheduled time as countdown - uses 'now' state for live updates
  let scheduledInfo = $derived.by(() => {
    if (!todo.scheduledFor) return null;

    const scheduled = todo.scheduledFor;
    const diff = scheduled.getTime() - now.getTime();

    if (diff <= 0) {
      return 'Due now';
    }

    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) {
      return `${days}d ${hours % 24}h`;
    } else if (hours > 0) {
      return `${hours}h ${minutes % 60}m`;
    } else if (minutes > 0) {
      return `${minutes}m`;
    }
    const seconds = Math.floor(diff / 1000);
    return `${seconds}s`;
  });

  // Check if there are expandable details
  let hasDetails = $derived(!!todo.notes);

  // Check if user-created
  let isUserCreated = $derived(todo.createdBy === 'user');

  // Recurrence label
  let recurrenceLabel = $derived.by(() => {
    if (!todo.recurrence) return null;
    const labels: Record<string, string> = {
      '5min': 'Every 5m',
      '10min': 'Every 10m',
      '15min': 'Every 15m',
      '30min': 'Every 30m',
      'hourly': 'Hourly',
      'daily': 'Daily',
      'weekly': 'Weekly',
      'monthly': 'Monthly'
    };
    return labels[todo.recurrence] || todo.recurrence;
  });

  function toggleExpand() {
    if (hasDetails) {
      expanded = !expanded;
    }
  }

  async function handleComplete(e: MouseEvent) {
    e.stopPropagation();
    if (todo.status === 'done' || completing) return;

    completing = true;
    try {
      await todosStore.complete(todo.id);
    } catch (err) {
      console.error('Failed to complete todo:', err);
    } finally {
      completing = false;
    }
  }

  function handleEdit(e: MouseEvent) {
    e.stopPropagation();
    onEdit?.(todo);
  }
</script>

<div
  class="todo-item"
  class:completed={todo.status === 'done'}
  class:scheduled={!!todo.scheduledFor}
  class:highlighted={highlighted}
  class:expandable={hasDetails}
  class:expanded={expanded}
  role="button"
  tabindex="0"
  onclick={toggleExpand}
  onkeydown={(e) => e.key === 'Enter' && toggleExpand()}
>
  <!-- Complete checkbox -->
  <button
    class="complete-btn"
    class:completing={completing}
    class:done={todo.status === 'done'}
    onclick={handleComplete}
    disabled={todo.status === 'done' || completing}
    type="button"
    title={todo.status === 'done' ? 'Completed' : 'Mark as done'}
  >
    {#if completing}
      <span class="spinner"></span>
    {:else if todo.status === 'done'}
      <Icon name="check" size={12} />
    {:else}
      <span class="checkbox-empty"></span>
    {/if}
  </button>

  <div class="todo-content">
    <div class="todo-header">
      {#if isUserCreated}
        <span class="creator-badge user" title="Created by you">
          <Icon name="user" size={10} />
        </span>
      {/if}
      <span class="todo-task">{todo.task}</span>
      {#if threadTitle}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <span
          class="thread-badge"
          class:clickable={!!onNavigateToThread}
          onclick={(e) => { if (onNavigateToThread) { e.stopPropagation(); onNavigateToThread(); } }}
        >{threadTitle}</span>
      {/if}
      <div class="badges">
        {#if recurrenceLabel}
          <span class="recurrence-badge" title="Recurring task">
            <Icon name="refresh" size={10} />
            {recurrenceLabel}
          </span>
        {/if}
        {#if scheduledInfo}
          <span class="scheduled-badge" title="Time until activation">
            <Icon name="clock" size={10} />
            {scheduledInfo}
          </span>
        {/if}
        {#if hasDetails}
          <span class="expand-icon" class:rotated={expanded}>
            <Icon name="chevronDown" size={12} />
          </span>
        {/if}
      </div>
    </div>

    <!-- Expandable details -->
    {#if expanded}
      <div class="todo-details">
        {#if todo.notes}
          <div class="detail-row notes">
            <Icon name="fileText" size={12} />
            <span>{todo.notes}</span>
          </div>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Edit button -->
  {#if onEdit}
    <button
      class="edit-btn"
      onclick={handleEdit}
      type="button"
      title="Edit task"
    >
      <Icon name="edit" size={12} />
    </button>
  {/if}
</div>

<style>
  .todo-item {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), transform var(--transition-fast);
    width: 100%;
    text-align: left;
    background: transparent;
    border: none;
    cursor: default;
    font-family: inherit;
    animation: staggerFadeIn 0.3s ease-out backwards;
  }

  .todo-item:nth-child(1) { animation-delay: 0.03s; }
  .todo-item:nth-child(2) { animation-delay: 0.06s; }
  .todo-item:nth-child(3) { animation-delay: 0.09s; }
  .todo-item:nth-child(4) { animation-delay: 0.12s; }
  .todo-item:nth-child(5) { animation-delay: 0.15s; }
  .todo-item:nth-child(6) { animation-delay: 0.18s; }
  .todo-item:nth-child(7) { animation-delay: 0.21s; }
  .todo-item:nth-child(8) { animation-delay: 0.24s; }

  .todo-item.expandable {
    cursor: pointer;
  }

  .todo-item:hover {
    background: var(--bg-hover);
  }

  .todo-item:hover .edit-btn {
    opacity: 1;
  }

  .todo-item.completed {
    opacity: 0.6;
  }

  .todo-item.completed .todo-task {
    text-decoration: line-through;
  }

  .todo-item.highlighted {
    background: rgba(var(--accent-primary-rgb), 0.08);
  }

  .todo-item.highlighted:hover {
    background: rgba(var(--accent-primary-rgb), 0.12);
  }

  .complete-btn {
    flex-shrink: 0;
    width: 18px;
    height: 18px;
    border-radius: var(--radius-sm);
    border: 2px solid var(--border-default);
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all var(--transition-fast);
    margin-top: 1px;
    padding: 0;
  }

  .complete-btn:hover:not(:disabled) {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
  }

  .complete-btn.done {
    border-color: var(--success);
    background: var(--success);
    color: white;
    animation: checkBounce 0.3s ease-out;
  }

  .complete-btn:disabled {
    cursor: default;
  }

  .complete-btn .checkbox-empty {
    width: 100%;
    height: 100%;
  }

  .complete-btn .spinner {
    width: 10px;
    height: 10px;
    border: 2px solid transparent;
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .todo-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .todo-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .creator-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: var(--radius-full);
    flex-shrink: 0;
  }

  .creator-badge.user {
    background: rgba(var(--accent-primary-rgb), 0.15);
    color: var(--accent-primary);
  }

  .todo-task {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .badges {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
    margin-left: auto;
  }

  .recurrence-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: var(--font-size-xs);
    color: var(--warning);
    background: rgba(var(--warning-rgb, 245, 158, 11), 0.1);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
  }

  .scheduled-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
  }

  .expand-icon {
    display: flex;
    align-items: center;
    color: var(--text-muted);
    transition: transform var(--transition-fast);
  }

  .expand-icon.rotated {
    transform: rotate(180deg);
  }

  .edit-btn {
    flex-shrink: 0;
    padding: var(--spacing-xs);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    opacity: 0;
    transition: all var(--transition-fast);
    border: none;
  }

  .edit-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  .todo-details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-xs);
    border-top: 1px solid var(--border-subtle);
    margin-top: var(--spacing-xs);
  }

  .detail-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .detail-row.notes span {
    white-space: pre-wrap;
    word-break: break-word;
  }

  .todo-item.scheduled {
    border-left: 2px solid var(--accent-primary);
    padding-left: calc(var(--spacing-sm) - 2px);
  }

  .thread-badge {
    display: inline-block;
    max-width: 100px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 500;
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 0;
  }

  .thread-badge.clickable {
    cursor: pointer;
  }

  .thread-badge.clickable:hover {
    background: rgba(var(--accent-primary-rgb), 0.2);
  }
</style>
