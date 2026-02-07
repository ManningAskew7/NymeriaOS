<script lang="ts">
  import { Icon } from '$lib/components/common';
  import type { Notification } from '$lib/types';

  interface Props {
    notification: Notification;
    onclick?: () => void;
    onDismiss?: () => void;
  }

  let { notification, onclick, onDismiss }: Props = $props();

  function formatTimeAgo(date: Date): string {
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / (1000 * 60));
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays === 1) return 'yesterday';
    return `${diffDays}d ago`;
  }

  function handleClick() {
    onclick?.();
  }

  function handleDismiss(e: MouseEvent) {
    e.stopPropagation();
    onDismiss?.();
  }
</script>

<button
  class="notification-item"
  class:unread={!notification.read}
  type="button"
  onclick={handleClick}
>
  <div class="notification-content">
    <div class="notification-icon">
      <Icon name="bell" size={16} />
    </div>
    <div class="notification-text">
      <p class="notification-summary">{notification.summary}</p>
      <span class="notification-time">{formatTimeAgo(notification.createdAt)}</span>
    </div>
  </div>
  {#if !notification.read}
    <button
      class="dismiss-btn"
      type="button"
      onclick={handleDismiss}
      title="Mark as read"
    >
      <Icon name="x" size={14} />
    </button>
  {/if}
</button>

<style>
  .notification-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    text-align: left;
    background: transparent;
    border-radius: var(--radius-md);
    transition: background var(--transition-fast);
    cursor: pointer;
  }

  .notification-item:hover {
    background: var(--bg-hover);
  }

  .notification-item.unread {
    background: var(--bg-elevated);
  }

  .notification-item.unread:hover {
    background: var(--bg-hover);
  }

  .notification-content {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
  }

  .notification-icon {
    flex-shrink: 0;
    color: var(--accent-primary);
    margin-top: 2px;
  }

  .notification-text {
    flex: 1;
    min-width: 0;
  }

  .notification-summary {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
    /* Allow text to wrap */
    word-wrap: break-word;
  }

  .unread .notification-summary {
    font-weight: 500;
  }

  .notification-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    display: block;
    margin-top: 2px;
  }

  .dismiss-btn {
    flex-shrink: 0;
    padding: var(--spacing-xs);
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
    opacity: 0;
  }

  .notification-item:hover .dismiss-btn {
    opacity: 1;
  }

  .dismiss-btn:hover {
    background: var(--bg-elevated);
    color: var(--text-primary);
  }
</style>
