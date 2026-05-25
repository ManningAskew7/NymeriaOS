<script lang="ts">
  import { slide } from 'svelte/transition';
  import { Icon } from '$lib/components/common';
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import NotificationItem from './NotificationItem.svelte';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
  }

  let { isOpen, onClose }: Props = $props();

  function handleNotificationClick(notification: { threadId?: string; id: string }) {
    // Navigate to thread if available
    if (notification.threadId) {
      threadsStore.selectThread(notification.threadId);
    }
    // Mark as read
    notificationStore.markRead(notification.id);
    onClose();
  }

  function handleDismiss(notificationId: string) {
    notificationStore.markRead(notificationId);
  }

  function handleDelete(notificationId: string) {
    notificationStore.deleteNotification(notificationId);
  }

  function handleMarkAllRead() {
    notificationStore.markAllRead();
  }

  function handleClearAll() {
    if (confirm('Clear all notifications? This cannot be undone.')) {
      notificationStore.clearAllNotifications();
    }
  }

  $effect(() => {
    if (isOpen) {
      notificationStore.fetch();
    }
  });
</script>

{#if isOpen}
  <div class="notification-center" role="dialog" aria-label="Notifications" transition:slide={DROPDOWN_TRANSITION}>
    <div class="notification-header">
      <h3 class="notification-title">Notifications</h3>
      <div class="header-actions">
        {#if notificationStore.unreadCount > 0}
          <button class="header-btn" type="button" onclick={handleMarkAllRead}>
            Mark all read
          </button>
        {/if}
        {#if notificationStore.notifications.length > 0}
          <button
            class="header-btn subtle"
            type="button"
            onclick={handleClearAll}
            title="Delete all notifications"
          >
            Clear
          </button>
        {/if}
      </div>
    </div>

    <div class="notification-list">
      {#if notificationStore.loading && notificationStore.notifications.length === 0}
        <div class="notification-empty">
          <Icon name="loading" size={24} />
          <span>Loading...</span>
        </div>
      {:else if notificationStore.notifications.length === 0}
        <div class="notification-empty">
          <Icon name="bell" size={24} />
          <span>No notifications</span>
        </div>
      {:else}
        {#each notificationStore.notifications as notification (notification.id)}
          <NotificationItem
            {notification}
            onclick={() => handleNotificationClick(notification)}
            onDismiss={() => handleDismiss(notification.id)}
            onDelete={() => handleDelete(notification.id)}
          />
        {/each}
      {/if}
    </div>
  </div>
{/if}

<style>
  .notification-center {
    position: absolute;
    bottom: 100%;
    left: 0;
    margin-bottom: var(--spacing-sm);
    width: max(100%, 280px);
    max-width: calc(100vw - 24px);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    max-height: min(400px, calc(100vh - 96px));
    display: flex;
    flex-direction: column;
    z-index: 999;
  }

  .notification-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .notification-title {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .header-actions {
    display: flex;
    gap: var(--spacing-xs);
  }

  .header-btn {
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .header-btn.subtle {
    color: var(--text-muted);
  }

  .header-btn:hover {
    background: var(--bg-hover);
  }

  .notification-list {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-xs);
  }

  .notification-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xl);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }
</style>
