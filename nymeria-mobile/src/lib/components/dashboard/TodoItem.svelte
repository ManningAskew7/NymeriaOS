<script lang="ts">
  import type { TodoItem as TodoItemType } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { todosStore } from '$lib/stores/todos.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { onDestroy } from 'svelte';

  interface Props {
    todo: TodoItemType;
    highlighted?: boolean;
    onEdit?: (todo: TodoItemType) => void;
    onComplete?: (id: string) => void;
    onDelete?: (id: string) => void;
    threadTitle?: string;
    onNavigateToThread?: () => void;
  }

  let {
    todo,
    highlighted = false,
    onEdit,
    onComplete,
    onDelete,
    threadTitle,
    onNavigateToThread,
  }: Props = $props();

  let completing = $state(false);

  // Reactive time for countdown
  let now = $state(new Date());
  let intervalId: ReturnType<typeof setInterval> | null = null;

  $effect(() => {
    if (todo.scheduledFor) {
      if (!intervalId) {
        intervalId = setInterval(() => { now = new Date(); }, 1000);
      }
    } else {
      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
      }
    }
  });

  onDestroy(() => {
    if (intervalId) clearInterval(intervalId);
  });

  let statusIcon = $derived.by(() => {
    switch (todo.status) {
      case 'in_progress': return 'loading';
      case 'done': return 'check';
      default: return 'clock';
    }
  });

  let statusColor = $derived.by(() => {
    switch (todo.status) {
      case 'in_progress': return 'var(--accent-primary)';
      case 'done': return 'var(--success)';
      default: return 'var(--text-muted)';
    }
  });

  // Scheduled countdown
  let scheduledInfo = $derived.by(() => {
    if (!todo.scheduledFor) return null;
    const diff = todo.scheduledFor.getTime() - now.getTime();
    if (diff <= 0) return 'Due now';

    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}d ${hours % 24}h`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m`;
    return `${Math.floor(diff / 1000)}s`;
  });

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

  async function handleComplete(e: MouseEvent) {
    e.stopPropagation();
    if (todo.status === 'done' || completing) return;

    completing = true;
    try {
      if (onComplete) {
        onComplete(todo.id);
      } else {
        await todosStore.complete(todo.id);
      }
    } catch (err) {
      console.error('Failed to complete todo:', err);
      // The checkbox row has no inline error slot; surface through the toast layer.
      errorsStore.push({
        kind: 'generic',
        message: humanizeErrorText(err, { action: 'complete', resource: 'the task' }),
      });
    } finally {
      completing = false;
    }
  }

  function handleDelete(e: MouseEvent) {
    e.stopPropagation();
    if (onDelete) {
      onDelete(todo.id);
    } else {
      todosStore.delete(todo.id);
    }
  }

  function handleTap() {
    if (onEdit) {
      onEdit(todo);
    }
  }

  function handleThreadBadgeClick(e: MouseEvent) {
    e.stopPropagation();
    onNavigateToThread?.();
  }

  // Keyboard activation for the row (now a role="button"). Nested controls
  // (complete / delete buttons) handle their own keys, so only open the edit
  // form when the row itself is the event target.
  function handleRowKeydown(e: KeyboardEvent) {
    if ((e.target as HTMLElement).closest('button')) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleTap();
    }
  }
</script>

<div
  class="todo-item"
  class:done={todo.status === 'done'}
  class:in-progress={todo.status === 'in_progress'}
  class:highlighted
  class:scheduled={!!todo.scheduledFor}
  class:editable={!!onEdit}
  role="button"
  tabindex="0"
  onclick={handleTap}
  onkeydown={handleRowKeydown}
>
  <!-- Complete checkbox -->
  <button
    class="complete-btn"
    class:completing
    class:checked={todo.status === 'done'}
    onclick={handleComplete}
    disabled={todo.status === 'done' || completing}
    type="button"
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
    <span class="todo-task">{todo.task}</span>
    <div class="todo-meta">
      {#if threadTitle}
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <span
          class="thread-badge"
          class:clickable={!!onNavigateToThread}
          onclick={handleThreadBadgeClick}
        >{threadTitle}</span>
      {/if}
      {#if recurrenceLabel}
        <span class="recurrence-badge">
          <Icon name="refresh" size={10} />
          {recurrenceLabel}
        </span>
      {/if}
      {#if scheduledInfo}
        <span class="scheduled-badge">
          <Icon name="clock" size={10} />
          {scheduledInfo}
        </span>
      {/if}
      {#if todo.notes}
        <span class="todo-notes">{todo.notes}</span>
      {/if}
    </div>
  </div>

  <!-- Inline actions (visible for non-done items) -->
  {#if todo.status !== 'done'}
    <button class="action-btn delete" onclick={handleDelete} title="Delete" type="button">
      <Icon name="trash" size={14} />
    </button>
  {/if}
</div>

<style>
  .todo-item {
    display: flex;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    min-height: var(--touch-target-min);
    align-items: flex-start;
    border-bottom: 1px solid var(--border-subtle);
  }

  .todo-item:active {
    background: var(--bg-hover);
  }

  .todo-item.editable {
    cursor: pointer;
  }

  /* Inset ring: rows are edge-to-edge in the feed, so the app.css outset
     baseline (+2px) is clip-prone. Mirrors the ThreadItem row treatment. */
  .todo-item:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .todo-item.done {
    opacity: 0.5;
  }

  .todo-item.highlighted {
    background: var(--accent-tint-bg);
  }

  .todo-item.scheduled {
    border-left: 2px solid var(--accent-primary);
    padding-left: calc(var(--spacing-md) - 2px);
  }

  /* Complete checkbox */
  .complete-btn {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border-radius: var(--radius-sm);
    border: 2px solid var(--border-default);
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-top: 2px;
    padding: 0;
  }

  .complete-btn:active:not(:disabled) {
    border-color: var(--accent-primary);
    background: var(--accent-tint-bg);
  }

  .complete-btn.checked {
    border-color: var(--success);
    background: var(--success);
    color: white;
  }

  .complete-btn:disabled {
    cursor: default;
  }

  .complete-btn .checkbox-empty {
    width: 100%;
    height: 100%;
  }

  .complete-btn .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid transparent;
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  .todo-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .todo-task {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
  }

  .done .todo-task {
    text-decoration: line-through;
    color: var(--text-muted);
  }

  .todo-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-wrap: wrap;
  }

  .todo-notes {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    display: -webkit-box;
    -webkit-line-clamp: 1;
    line-clamp: 1;
    -webkit-box-orient: vertical;
    overflow: hidden;
    width: 100%;
  }

  .thread-badge {
    display: inline-block;
    max-width: 120px;
    padding: 1px 6px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 0;
  }

  .thread-badge.clickable:active {
    opacity: 0.7;
  }

  .recurrence-badge {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }

  .scheduled-badge {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    padding: 0 4px;
    border-radius: var(--radius-sm);
  }

  .action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: var(--radius-md);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .action-btn:active {
    background: var(--bg-hover);
  }

  .action-btn.delete {
    color: var(--error);
    opacity: 0.6;
  }

  .action-btn.delete:active {
    opacity: 1;
  }
</style>
