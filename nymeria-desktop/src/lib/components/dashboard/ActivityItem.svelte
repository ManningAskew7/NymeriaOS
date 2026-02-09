<script lang="ts">
  import type { ActivityEntry, ActivityType } from '$lib/types';
  import { Icon } from '$lib/components/common';

  interface Props {
    entry: ActivityEntry;
  }

  let { entry }: Props = $props();

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

<div class="activity-item">
  <div class="activity-icon" style="color: {color}">
    <Icon name={icon} size={12} />
  </div>

  <div class="activity-content">
    <span class="activity-message">{entry.message}</span>
    <span class="activity-time">{timeAgo}</span>
  </div>
</div>

<style>
  .activity-item {
    display: flex;
    gap: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
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
  }

  .activity-time {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
