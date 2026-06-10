<script lang="ts">
  import type { Notification } from '$lib/types';
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import NotificationItem from './NotificationItem.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';

  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

  function handleNotificationTap(notification: Notification) {
    if (notification.threadId) {
      switchToThread(notification.threadId);
    }
    onClose();
  }

  function handleMarkRead(id: string) {
    notificationStore.markRead(id);
  }

  function handleMarkAllRead() {
    notificationStore.markAllRead();
  }

  function handleOverlayClick(e: MouseEvent) {
    if (e.target === e.currentTarget) onClose();
  }

  // §6 a11y — Escape closes the bottom-sheet modal while open. Window
  // listener (vs onkeydown on the overlay) so the handler fires no
  // matter where focus currently sits inside the sheet.
  $effect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });
</script>

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="notification-overlay" onclick={handleOverlayClick} role="presentation">
    <div class="notification-sheet">
      <div class="sheet-handle"></div>

      <div class="sheet-header">
        <h3>Notifications</h3>
        {#if notificationStore.unreadCount > 0}
          <button class="mark-all-btn" onclick={handleMarkAllRead}>
            Mark all read
          </button>
        {/if}
      </div>

      <div class="sheet-body">
        {#if notificationStore.loading && notificationStore.notifications.length === 0}
          <div class="empty-state">
            <Spinner size="md" />
            <span>Loading notifications…</span>
          </div>
        {:else if notificationStore.notifications.length === 0}
          <div class="empty-state">
            <Icon name="bell" size={32} />
            <p>No notifications yet. Task and trigger updates will land here.</p>
          </div>
        {:else}
          {#each notificationStore.notifications as notification (notification.id)}
            <NotificationItem
              {notification}
              onRead={handleMarkRead}
              onTap={handleNotificationTap}
            />
          {/each}
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .notification-overlay {
    position: fixed;
    inset: 0;
    z-index: 200;
    background: rgba(0, 0, 0, 0.3);
    display: flex;
    align-items: flex-end;
    justify-content: center;
    animation: fadeIn 150ms ease;
  }

  .notification-sheet {
    width: 100%;
    max-width: 500px;
    max-height: 70vh;
    background: var(--bg-elevated);
    border-top-left-radius: var(--radius-xl);
    border-top-right-radius: var(--radius-xl);
    display: flex;
    flex-direction: column;
    animation: slideUp 200ms ease;
  }

  .sheet-handle {
    width: 36px;
    height: 4px;
    border-radius: 2px;
    background: var(--border-subtle);
    margin: var(--spacing-sm) auto;
    flex-shrink: 0;
  }

  .sheet-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .sheet-header h3 {
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
  }

  .mark-all-btn {
    font-size: var(--font-size-sm);
    color: var(--accent-primary);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
  }

  .mark-all-btn:active {
    background: var(--accent-tint-bg);
  }

  .sheet-body {
    flex: 1;
    overflow-y: auto;
    overscroll-behavior-y: contain;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xl);
    gap: var(--spacing-sm);
    color: var(--text-muted);
  }

  .empty-state p {
    font-size: var(--font-size-base);
    color: var(--text-secondary);
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes slideUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }
</style>
