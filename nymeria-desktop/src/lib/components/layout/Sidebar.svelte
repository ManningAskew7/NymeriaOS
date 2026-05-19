<script lang="ts">
  import { onMount } from 'svelte';
  import { Button, Icon, Modal, SettingsPanel } from '$lib/components/common';
  import { NotificationCenter } from '$lib/components/notifications';
  import { AccountBadge } from '$lib/components/account';
  import ThreadList from '$lib/components/threads/ThreadList.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import { uiStore } from '$lib/stores/ui.svelte';

  let showSettings = $state(false);
  let settingsInitialTab = $state<string | undefined>(undefined);
  let showNotifications = $state(false);
  let notificationWrapper: HTMLDivElement | undefined;

  let isCollapsed = $derived(uiStore.sidebarCollapsed);

  function handleNewChat() {
    threadsStore.createThread();
    chatStore.clearMessages();
  }

  function openSettings(tab?: string) {
    settingsInitialTab = tab;
    showSettings = true;
  }

  function closeSettings() {
    showSettings = false;
    settingsInitialTab = undefined;
  }

  function toggleNotifications(e: MouseEvent) {
    e.stopPropagation();
    showNotifications = !showNotifications;
  }

  function closeNotifications() {
    showNotifications = false;
  }

  function handleNotificationDocumentClick(e: MouseEvent) {
    const target = e.target;
    if (!(target instanceof Node) || !notificationWrapper?.contains(target)) {
      closeNotifications();
    }
  }

  function handleNotificationDocumentKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      closeNotifications();
    }
  }

  $effect(() => {
    if (showNotifications) {
      document.addEventListener('click', handleNotificationDocumentClick, true);
      document.addEventListener('keydown', handleNotificationDocumentKeydown, true);
      return () => {
        document.removeEventListener('click', handleNotificationDocumentClick, true);
        document.removeEventListener('keydown', handleNotificationDocumentKeydown, true);
      };
    }
  });

  // Start notification polling on mount
  onMount(() => {
    notificationStore.startPolling();
    return () => {
      notificationStore.stopPolling();
    };
  });
</script>

<div class="sidebar-content" class:collapsed={isCollapsed}>
  <div class="sidebar-header" class:collapsed={isCollapsed}>
    {#if !isCollapsed}
      <div class="brand">
        <img src="/wolfhead-transparent.png" alt="" class="brand-mark" />
        <h1 class="logo">Nymeria</h1>
      </div>
      <Button variant="primary" size="sm" onclick={handleNewChat}>
        <Icon name="plus" size={16} />
        New Thread
      </Button>
    {:else}
      <button
        class="icon-btn"
        type="button"
        onclick={handleNewChat}
        title="New Chat"
        aria-label="New Chat"
      >
        <Icon name="plus" size={20} />
      </button>
    {/if}
  </div>

  {#if !isCollapsed}
    <div class="threads-container">
      <ThreadList />
    </div>
  {/if}

  <div class="sidebar-footer" class:collapsed={isCollapsed}>
    <div class="notification-wrapper" bind:this={notificationWrapper}>
      {#if !isCollapsed}
        <button
          class="footer-btn"
          class:has-unread={notificationStore.unreadCount > 0}
          type="button"
          onclick={toggleNotifications}
          aria-haspopup="dialog"
          aria-expanded={showNotifications}
        >
          <Icon name="bell" size={18} />
          Notifications
          {#if notificationStore.unreadCount > 0}
            <span class="notification-badge">{notificationStore.unreadCount}</span>
          {/if}
        </button>
      {:else}
        <button
          class="icon-btn"
          class:has-unread={notificationStore.unreadCount > 0}
          type="button"
          onclick={toggleNotifications}
          title="Notifications"
          aria-label="Notifications"
          aria-haspopup="dialog"
          aria-expanded={showNotifications}
        >
          <Icon name="bell" size={20} />
          {#if notificationStore.unreadCount > 0}
            <span class="notification-badge-collapsed">{notificationStore.unreadCount}</span>
          {/if}
        </button>
      {/if}
      <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} />
    </div>
    <AccountBadge onOpenSettings={openSettings} />
    {#if !isCollapsed}
      <button class="footer-btn" type="button" onclick={() => openSettings()}>
        <Icon name="settings" size={18} />
        Settings
      </button>
    {:else}
      <button
        class="icon-btn"
        type="button"
        onclick={() => openSettings()}
        title="Settings"
        aria-label="Settings"
      >
        <Icon name="settings" size={20} />
      </button>
    {/if}
  </div>
</div>

<Modal title="Settings" isOpen={showSettings} onClose={closeSettings}>
  <SettingsPanel initialTab={settingsInitialTab} />
</Modal>

<style>
  .sidebar-content {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow: hidden;
  }

  .sidebar-content.collapsed {
    align-items: center;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
  }

  .sidebar-header.collapsed {
    justify-content: center;
    padding: var(--spacing-md) var(--spacing-sm);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  .brand-mark {
    width: 2.5em;
    height: 2.5em;
    object-fit: contain;
    flex-shrink: 0;
    filter: drop-shadow(0 0 6px rgba(34, 211, 238, 0.25));
  }

  :global(html[data-theme='light']) .brand-mark {
    filter: invert(1);
  }

  .logo {
    font-size: var(--font-size-xl);
    font-weight: 700;
    color: var(--accent-primary);
    margin: 0;
    letter-spacing: -0.02em;
    text-shadow: 0 0 20px rgba(34, 211, 238, 0.2);
  }

  .threads-container {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .sidebar-footer {
    padding: calc(var(--spacing-md) + 5px) var(--spacing-md) var(--spacing-md);
    border-top: 1px solid var(--glass-border);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .sidebar-footer.collapsed {
    padding: var(--spacing-md) var(--spacing-sm);
    align-items: center;
    width: 100%;
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
    transform: translateX(2px);
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
    animation: glowPulse 2s ease-in-out infinite;
  }

  /* Icon-only buttons for collapsed state */
  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    color: var(--text-secondary);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
    position: relative;
  }

  .icon-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .icon-btn.has-unread {
    color: var(--accent-primary);
  }

  /* Notification badge for collapsed icon button */
  .notification-badge-collapsed {
    position: absolute;
    top: 2px;
    right: 2px;
    min-width: 16px;
    height: 16px;
    padding: 0 3px;
    font-size: 10px;
    font-weight: 600;
    color: white;
    background: var(--accent-primary);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: glowPulse 2s ease-in-out infinite;
  }
</style>
