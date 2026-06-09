<script lang="ts">
  import type { Notification } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    notification: Notification;
    onRead: (id: string) => void;
    onTap: (notification: Notification) => void;
  }

  let { notification, onRead, onTap }: Props = $props();

  function formatTime(date: Date): string {
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'Just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function handleTap() {
    if (!notification.read) onRead(notification.id);
    onTap(notification);
  }
</script>

<button class="notification-item" class:unread={!notification.read} onclick={handleTap}>
  <div class="notif-content">
    {#if !notification.read}
      <span class="unread-dot"></span>
    {/if}
    <p class="notif-summary">{notification.summary}</p>
    <span class="notif-time">{formatTime(notification.createdAt)}</span>
  </div>
</button>

<style>
  .notification-item {
    display: flex;
    width: 100%;
    padding: var(--spacing-md) var(--spacing-lg);
    text-align: left;
    border-bottom: 1px solid var(--border-subtle);
    transition: background var(--transition-fast);
    min-height: var(--touch-target-min);
  }

  .notification-item:active {
    background: var(--bg-hover);
  }

  .notification-item.unread {
    background: var(--accent-tint-bg);
  }

  .notif-content {
    display: flex;
    flex-direction: column;
    gap: 2px;
    position: relative;
  }

  .unread-dot {
    position: absolute;
    top: 4px;
    left: -12px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-primary);
  }

  .notif-summary {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
  }

  .notif-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }
</style>
