<script lang="ts">
  import type { TodoItem } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { onMount, onDestroy } from 'svelte';

  interface Props {
    todo: TodoItem;
  }

  let { todo }: Props = $props();

  // Reactive time for countdown - updates every second
  let now = $state(new Date());
  let intervalId: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    // Update time every second for countdown
    intervalId = setInterval(() => {
      now = new Date();
    }, 1000);
  });

  onDestroy(() => {
    if (intervalId) {
      clearInterval(intervalId);
    }
  });

  // Calculate time until execution - now reactive because 'now' updates
  let timeUntil = $derived.by(() => {
    if (!todo.scheduledFor) return '';

    const diff = todo.scheduledFor.getTime() - now.getTime();

    if (diff <= 0) {
      return todo.status === 'in_progress' ? 'Running now' : 'Due now';
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

  let statusIcon = $derived(todo.status === 'in_progress' ? 'loading' : 'clock');
  let statusColor = $derived(
    todo.status === 'in_progress' ? 'var(--accent-primary)' : 'var(--text-muted)'
  );

  // Priority indicator
  let priorityBadge = $derived.by(() => {
    if (!todo.priority) return null;
    switch (todo.priority) {
      case 'high':
        return { text: '!!', color: 'var(--error)' };
      case 'medium':
        return { text: '!', color: 'var(--warning)' };
      default:
        return null;
    }
  });
</script>

<div class="todo-item" class:running={todo.status === 'in_progress'}>
  <div class="todo-icon" style="color: {statusColor}">
    <Icon name={statusIcon} size={14} />
  </div>

  <div class="todo-content">
    <div class="todo-header">
      {#if priorityBadge}
        <span class="priority-badge" style="color: {priorityBadge.color}">{priorityBadge.text}</span>
      {/if}
      <span class="todo-task">{todo.task}</span>
    </div>
    <span class="todo-time">{timeUntil}</span>
  </div>
</div>

<style>
  .todo-item {
    display: flex;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm);
    border-radius: var(--radius-md);
    transition: background var(--transition-fast);
    border-left: 2px solid var(--accent-primary);
  }

  .todo-item:hover {
    background: var(--bg-hover);
  }

  .todo-item.running {
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
  }

  .todo-icon {
    flex-shrink: 0;
    margin-top: 2px;
  }

  .todo-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .todo-header {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-xs);
  }

  .priority-badge {
    font-size: var(--font-size-xs);
    font-weight: 700;
    flex-shrink: 0;
  }

  .todo-task {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .todo-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .running .todo-time {
    color: var(--accent-primary);
  }
</style>
