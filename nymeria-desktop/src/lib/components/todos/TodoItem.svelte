<script lang="ts">
  import type { TodoItem as TodoItemType } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { onMount, onDestroy } from 'svelte';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';

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

  // Recurrence label — mirrors Nymeria.core.todo_constants.format_recurrence_for_display.
  let recurrenceLabel = $derived.by(() => {
    if (!todo.recurrence) return null;
    const legacy: Record<string, string> = {
      '5min': '5m', '10min': '10m', '15min': '15m', '30min': '30m',
      hourly: '1h', daily: '1d', weekly: '1w', monthly: '1mo'
    };
    const canonical = legacy[todo.recurrence.toLowerCase()] ?? todo.recurrence.toLowerCase();
    if (canonical === '1h') return 'Hourly';
    if (canonical === '1d') return 'Daily';
    if (canonical === '1w') return 'Weekly';
    if (canonical === '1mo') return 'Monthly';
    if (/^\d+(mo|s|m|h|d|w)$/.test(canonical)) return `Every ${canonical}`;
    return todo.recurrence;
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
      {#if hasDetails}
        <span class="expand-icon" class:rotated={expanded} aria-hidden="true">
          <Icon name="chevronDown" size={12} />
        </span>
      {/if}
    </div>

    {#if threadTitle || recurrenceLabel || scheduledInfo}
      <div class="todo-meta">
        {#if threadTitle}
          {#if onNavigateToThread}
            <button
              class="meta-thread clickable"
              type="button"
              title={threadTitle}
              onclick={(e) => { e.stopPropagation(); onNavigateToThread(); }}
            >{threadTitle}</button>
          {:else}
            <span class="meta-thread" title={threadTitle}>{threadTitle}</span>
          {/if}
        {/if}
        {#if recurrenceLabel}
          {#if threadTitle}<span class="meta-sep" aria-hidden="true">·</span>{/if}
          <span class="meta-item" title="Recurring task">
            <Icon name="refresh" size={10} />
            {recurrenceLabel}
          </span>
        {/if}
        {#if scheduledInfo}
          {#if threadTitle || recurrenceLabel}<span class="meta-sep" aria-hidden="true">·</span>{/if}
          <span class="meta-item meta-countdown" title="Time until activation">
            <Icon name="clock" size={10} />
            {scheduledInfo}
          </span>
        {/if}
      </div>
    {/if}

    <!-- Expandable details — slide-down to match the trigger card animation -->
    {#if expanded}
      <div class="todo-details" transition:slide={{ duration: 120, easing: cubicOut, axis: 'y' }}>
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
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: 12px var(--spacing-md) 12px 14px;
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), border-color var(--transition-fast);
    width: 100%;
    text-align: left;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    cursor: default;
    font-family: inherit;
    animation: staggerFadeIn 0.3s ease-out backwards;
  }

  .todo-item:focus-visible {
    outline: 1px solid var(--accent-primary);
    outline-offset: 1px;
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
    border-color: var(--border-default);
  }

  .todo-item:hover .edit-btn {
    opacity: 1;
  }

  .todo-item.completed {
    opacity: 0.55;
  }

  .todo-item.completed .todo-task {
    text-decoration: line-through;
    text-decoration-color: var(--text-muted);
    text-decoration-thickness: 1px;
  }

  .todo-item.highlighted {
    background: rgba(var(--accent-primary-rgb), 0.08);
    border-color: rgba(var(--accent-primary-rgb), 0.35);
  }

  .todo-item.highlighted:hover {
    background: rgba(var(--accent-primary-rgb), 0.12);
  }

  /* Scheduled accent — a thin inside rail rather than a chunky border */
  .todo-item.scheduled::before {
    content: '';
    position: absolute;
    left: 0;
    top: 10px;
    bottom: 10px;
    width: 2px;
    background: var(--accent-primary);
    border-radius: 0 1px 1px 0;
    opacity: 0.55;
    transition: opacity var(--transition-fast);
  }

  .todo-item.scheduled:hover::before,
  .todo-item.highlighted::before {
    opacity: 1;
  }

  .complete-btn {
    flex-shrink: 0;
    width: 13px;
    height: 13px;
    border-radius: var(--radius-sm);
    border: 1.5px solid var(--border-default);
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all var(--transition-fast);
    margin-top: 2px;
    padding: 0;
  }

  .complete-btn:hover:not(:disabled) {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.12);
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
    gap: 6px;
  }

  .todo-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    min-width: 0;
  }

  .creator-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: var(--radius-full);
    flex-shrink: 0;
    color: var(--accent-primary);
    opacity: 0.7;
  }

  .todo-task {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    line-height: 1.35;
    letter-spacing: -0.005em;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }


  /* Quiet secondary metadata row — the key to the professional look */
  .todo-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.5;
  }

  .meta-thread {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-secondary);
    font: inherit;
    font-weight: 500;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex-shrink: 1;
    min-width: 0;
    transition: color var(--transition-fast);
  }

  .meta-thread.clickable {
    cursor: pointer;
  }

  .meta-thread.clickable:hover {
    color: var(--accent-primary);
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .meta-countdown {
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .highlighted .meta-countdown {
    color: var(--accent-primary);
  }

  .meta-sep {
    color: var(--text-muted);
    opacity: 0.5;
    flex-shrink: 0;
    user-select: none;
  }

  .expand-icon {
    display: flex;
    align-items: center;
    color: var(--text-muted);
    flex-shrink: 0;
    transition: transform var(--transition-fast);
  }

  .expand-icon.rotated {
    transform: rotate(180deg);
  }

  .edit-btn {
    flex-shrink: 0;
    padding: 4px;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    opacity: 0;
    transition: all var(--transition-fast);
    border: none;
    align-self: flex-start;
    margin-top: 2px;
  }

  .edit-btn:hover {
    background: var(--bg-active);
    color: var(--text-primary);
    opacity: 1;
  }

  .todo-details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-xs);
    border-top: 1px solid var(--border-subtle);
    margin-top: 4px;
  }

  .detail-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .detail-row.notes span {
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
