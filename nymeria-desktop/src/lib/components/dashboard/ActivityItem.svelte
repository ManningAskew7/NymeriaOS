<script lang="ts">
  import type { ActivityEntry } from '$lib/types';
  import { Icon } from '$lib/components/common';

  interface Props {
    entry: ActivityEntry;
    threadTitle?: string;
    onNavigate?: () => void;
  }

  let { entry, threadTitle, onNavigate }: Props = $props();

  // Get icon and color based on activity type
  let icon = $derived.by(() => {
    switch (entry.type) {
      case 'self_invoke':
        return 'clock';
      case 'watchdog_nudge':
        return 'warning';
      case 'task_completed':
        return 'check';
      case 'task_failed':
        return 'close';
      case 'todo_added':
        return 'plus';
      case 'todo_updated':
        return 'edit';
      case 'todo_completed':
        return 'check';
      case 'todo_deleted':
        return 'trash';
      case 'trigger_completed':
        return 'bolt';
      default:
        return 'info';
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

  // Format timestamp
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

{#snippet activityContent()}
  <div class="activity-icon" style="color: {color}">
    <Icon name={icon} size={12} />
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
{/snippet}

{#if onNavigate}
  <button class="activity-item clickable" type="button" onclick={onNavigate}>
    {@render activityContent()}
  </button>
{:else}
  <div class="activity-item">
    {@render activityContent()}
  </div>
{/if}

<style>
  .activity-item {
    display: flex;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    transition: background var(--transition-fast), transform var(--transition-fast);
    animation: staggerFadeIn 0.3s ease-out backwards;
  }

  .activity-item:nth-child(1) { animation-delay: 0.03s; }
  .activity-item:nth-child(2) { animation-delay: 0.06s; }
  .activity-item:nth-child(3) { animation-delay: 0.09s; }
  .activity-item:nth-child(4) { animation-delay: 0.12s; }
  .activity-item:nth-child(5) { animation-delay: 0.15s; }
  .activity-item:nth-child(6) { animation-delay: 0.18s; }
  .activity-item:nth-child(7) { animation-delay: 0.21s; }
  .activity-item:nth-child(8) { animation-delay: 0.24s; }
  .activity-item:nth-child(9) { animation-delay: 0.27s; }
  .activity-item:nth-child(10) { animation-delay: 0.3s; }

  .activity-item:hover {
    background: var(--bg-hover);
    transform: translateX(2px);
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
    gap: 1px;
  }

  .activity-message {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    transition: -webkit-line-clamp 0s;
  }

  .activity-item:hover .activity-message {
    -webkit-line-clamp: unset;
    line-clamp: unset;
    overflow: visible;
    display: block;
    word-break: break-word;
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
    font-size: 10px;
    font-weight: 500;
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    border-radius: var(--radius-sm);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .activity-time {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
