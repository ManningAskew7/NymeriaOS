<script lang="ts">
  import type { ActivityEntry } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    entry: ActivityEntry;
    threadTitle?: string;
    onNavigate?: () => void;
  }

  let { entry, threadTitle, onNavigate }: Props = $props();

  let icon = $derived.by(() => {
    switch (entry.type) {
      case 'self_invoke': return 'clock';
      case 'watchdog_nudge': return 'warning';
      case 'task_completed': return 'check';
      case 'task_failed': return 'close';
      case 'todo_added': return 'plus';
      case 'todo_updated': return 'edit';
      case 'todo_completed': return 'check';
      case 'todo_deleted': return 'trash';
      case 'trigger_completed': return 'bolt';
      default: return 'info';
    }
  });

  let color = $derived.by(() => {
    switch (entry.type) {
      case 'task_completed':
      case 'todo_completed':
        return 'var(--success)';
      case 'task_failed':
        return 'var(--error)';
      case 'watchdog_nudge':
        return 'var(--warning)';
      case 'self_invoke':
        return 'var(--accent-primary)';
      case 'trigger_completed':
        return 'var(--accent-secondary, var(--accent-primary))';
      default:
        return 'var(--text-muted)';
    }
  });

  let timeAgo = $derived.by(() => {
    const now = new Date();
    const diff = now.getTime() - entry.timestamp.getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'Just now';
  });
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
  class="activity-item"
  class:clickable={!!onNavigate}
  onclick={onNavigate}
>
  <div class="activity-icon" style="color: {color}">
    <Icon name={icon} size={14} />
  </div>

  <div class="activity-content">
    <span class="activity-message">{entry.message}</span>
    <div class="activity-meta">
      {#if threadTitle}
        <span class="thread-badge">{threadTitle}</span>
      {/if}
      <span class="activity-time">{timeAgo}</span>
    </div>
  </div>
</div>

<style>
  .activity-item {
    display: flex;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    min-height: 40px;
    align-items: flex-start;
  }

  .activity-item:active {
    background: var(--bg-hover);
  }

  .activity-item.clickable {
    cursor: pointer;
  }

  .activity-icon {
    flex-shrink: 0;
    margin-top: 3px;
    opacity: 0.8;
  }

  .activity-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .activity-message {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .activity-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .thread-badge {
    display: inline-block;
    max-width: 120px;
    padding: 1px 6px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--accent-primary);
    background: var(--accent-primary-alpha);
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .activity-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }
</style>
