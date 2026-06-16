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
  let notificationWrapper = $state<HTMLDivElement | undefined>(undefined);

  let isCollapsed = $derived(uiStore.sidebarCollapsed);

  function handleNewThread() {
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
  <header class="sidebar-header" class:collapsed={isCollapsed}>
    {#if !isCollapsed}
      <div class="brand">
        <img src="/wolfhead-transparent.png" alt="" class="brand-mark" />
        <h1 class="logo"><span class="logo-name">Nymeria</span><span class="logo-os">OS</span></h1>
      </div>
      <span class="new-thread-wrap">
        <Button variant="primary" size="sm" onclick={handleNewThread}>
          <Icon name="plus" size={14} />
          <span class="new-thread-label">New Thread</span>
        </Button>
      </span>
    {:else}
      <button
        class="icon-btn"
        type="button"
        onclick={handleNewThread}
        title="New Thread"
        aria-label="New Thread"
      >
        <Icon name="plus" size={20} />
      </button>
    {/if}
  </header>

  {#if !isCollapsed}
    <nav class="threads-container" aria-label="Threads">
      <ThreadList />
    </nav>
  {/if}

  <footer class="sidebar-footer" class:collapsed={isCollapsed}>
    {#if !isCollapsed}
      <div class="footer-row">
        <div class="footer-account">
          <AccountBadge onOpenSettings={openSettings} />
        </div>
        <div class="footer-tools">
          <div class="notification-wrapper" bind:this={notificationWrapper}>
            <button
              class="footer-icon-btn"
              class:has-unread={notificationStore.unreadCount > 0}
              type="button"
              onclick={toggleNotifications}
              title="Notifications"
              aria-label="Notifications"
              aria-haspopup="dialog"
              aria-expanded={showNotifications}
            >
              <Icon name="bell" size={16} />
              {#if notificationStore.unreadCount > 0}
                <span class="notification-badge-collapsed">{notificationStore.unreadCount}</span>
              {/if}
            </button>
            <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} />
          </div>
          <button
            class="footer-icon-btn"
            type="button"
            onclick={() => openSettings()}
            title="Settings"
            aria-label="Settings"
          >
            <Icon name="settings" size={16} />
          </button>
        </div>
      </div>
    {:else}
      <div class="notification-wrapper" bind:this={notificationWrapper}>
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
        <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} />
      </div>
      <AccountBadge onOpenSettings={openSettings} />
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
  </footer>
</div>

<Modal title="Global Settings" isOpen={showSettings} onClose={closeSettings}>
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
    filter: drop-shadow(0 0 6px rgba(var(--accent-primary-rgb), 0.25));
  }

  :global(html[data-theme='light']) .brand-mark {
    filter: invert(1);
  }

  .logo {
    font-family: var(--font-logo);
    font-size: var(--logo-font-size, var(--font-size-xl));
    color: var(--logo-color, var(--accent-primary));
    margin: 0;
    letter-spacing: -0.02em;
  }

  /* "Nymeria" — independently controllable weight + opacity. Kept as a span
     inside .logo so the two halves don't compose opacity multiplicatively. */
  .logo-name {
    font-weight: var(--logo-weight-name, 700);
    opacity: var(--logo-opacity-name, 1);
  }

  .logo-os {
    color: var(--logo-color, var(--accent-primary));
    font-weight: var(--logo-weight-os, 300);
    opacity: var(--logo-opacity-os, 1);
    text-shadow: none;
  }

  .new-thread-wrap :global(.btn) {
    /* Nudge the whole pill (background, + icon, and label) 1px down for
       optical alignment with neighbouring sidebar elements. Size stays the
       component's btn-sm; no token overrides. */
    transform: translateY(1px);
  }

  .new-thread-wrap :global(.new-thread-label) {
    transform: translateY(0);
  }

  .threads-container {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .sidebar-footer {
    padding: var(--spacing-md) var(--spacing-md) var(--spacing-md);
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

  .footer-row {
    display: flex;
    align-items: center;
    gap: 4px;
    width: 100%;
  }

  .footer-account {
    flex: 1;
    min-width: 0;
  }

  .footer-tools {
    display: flex;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }

  .footer-icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    position: relative;
    transition: color var(--transition-fast), background var(--transition-fast), transform var(--transition-fast);
  }

  .footer-icon-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  /* Pressed cue: a small scale-down reads as a tactile press on these
     icon-only buttons (real <button>s, so :active never fires when disabled). */
  .footer-icon-btn:active {
    transform: scale(0.92);
  }

  .footer-icon-btn.has-unread {
    color: var(--accent-primary);
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

  .icon-btn:active {
    transform: scale(0.92);
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
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: var(--text-on-accent);
    background: var(--accent-primary);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: glowPulse 2s ease-in-out infinite;
  }
</style>
