<script lang="ts">
  import { onMount } from 'svelte';
  import { Button, Icon, Modal, SettingsPanel } from '$lib/components/common';
  import { NotificationCenter } from '$lib/components/notifications';
  import ThreadList from '$lib/components/threads/ThreadList.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { notificationStore } from '$lib/stores/notifications.svelte';

  let showSettings = $state(false);
  let showNotifications = $state(false);

  function handleNewChat() {
    threadsStore.createThread();
    chatStore.clearMessages();
  }

  function openSettings() {
    showSettings = true;
  }

  function closeSettings() {
    showSettings = false;
  }

  function toggleNotifications(e: MouseEvent) {
    e.stopPropagation();
    showNotifications = !showNotifications;
  }

  function closeNotifications() {
    showNotifications = false;
  }

  // Start notification polling on mount
  onMount(() => {
    notificationStore.startPolling();
    return () => {
      notificationStore.stopPolling();
    };
  });
</script>

<div class="sidebar-content">
  <div class="sidebar-header">
    <h1 class="logo">Nymeria</h1>
    <Button variant="primary" size="sm" onclick={handleNewChat}>
      <Icon name="plus" size={16} />
      New Chat
    </Button>
  </div>

  <div class="threads-container">
    <ThreadList />
  </div>

  <div class="sidebar-footer">
    <div class="notification-wrapper">
      <button
        class="footer-btn"
        class:has-unread={notificationStore.unreadCount > 0}
        type="button"
        onclick={toggleNotifications}
      >
        <Icon name="bell" size={18} />
        Notifications
        {#if notificationStore.unreadCount > 0}
          <span class="notification-badge">{notificationStore.unreadCount}</span>
        {/if}
      </button>
      <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} />
    </div>
    <button class="footer-btn" type="button" onclick={openSettings}>
      <Icon name="settings" size={18} />
      Settings
    </button>
  </div>
</div>

<Modal title="Settings" isOpen={showSettings} onClose={closeSettings}>
  <SettingsPanel />
</Modal>

<style>
  .sidebar-content {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .logo {
    font-size: var(--font-size-xl);
    font-weight: 600;
    color: var(--accent-primary);
    margin: 0;
  }

  .threads-container {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .sidebar-footer {
    padding: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .notification-wrapper {
    position: relative;
  }

  .footer-btn {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
  }

  .footer-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .footer-btn.has-unread {
    color: var(--accent-primary);
  }

  .notification-badge {
    margin-left: auto;
    min-width: 18px;
    height: 18px;
    padding: 0 var(--spacing-xs);
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: white;
    background: var(--accent-primary);
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
</style>
