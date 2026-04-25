<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import ConnectionStatus from '$lib/components/common/ConnectionStatus.svelte';
  import SettingsPanel from '$lib/components/common/SettingsPanel.svelte';
  import { ThreadList } from '$lib/components/threads';
  import { NotificationCenter } from '$lib/components/notifications';
  import { AccountBadge } from '$lib/components/account';
  import { uiStore } from '$lib/stores/ui.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { notificationStore } from '$lib/stores/notifications.svelte';

  let showNotifications = $state(false);
  let showSettings = $state(false);
  let settingsInitialTab = $state<string | undefined>(undefined);

  function openSettings(tab?: string) {
    settingsInitialTab = tab;
    showSettings = true;
  }

  function closeSettings() {
    showSettings = false;
    settingsInitialTab = undefined;
  }

  function handleNewChat() {
    const thread = threadsStore.createThread();
    threadsStore.selectThread(thread.id);
    chatStore.clearMessages();
    uiStore.goToChat();
  }

  function toggleNotifications() {
    showNotifications = !showNotifications;
    if (showNotifications && !notificationStore.lastFetch) {
      notificationStore.fetch();
    }
  }
</script>

<div class="left-panel">
  <div class="panel-header">
    <h2>Nymeria</h2>
    <button
      class="new-chat-btn"
      onclick={handleNewChat}
      title="New Chat"
    >
      <Icon name="plus" size={22} />
    </button>
  </div>

  <div class="panel-body">
    <ThreadList />
  </div>

  <div class="panel-footer">
    <ConnectionStatus />

    <div class="footer-actions">
      <AccountBadge onOpenSettings={openSettings} />
      <button class="footer-btn" title="Settings" onclick={() => openSettings()}>
        <Icon name="settings" size={20} />
      </button>
      <button
        class="footer-btn"
        class:has-unread={notificationStore.unreadCount > 0}
        onclick={toggleNotifications}
        title="Notifications"
      >
        <Icon name="bell" size={20} />
        {#if notificationStore.unreadCount > 0}
          <span class="badge">{notificationStore.unreadCount > 9 ? '9+' : notificationStore.unreadCount}</span>
        {/if}
      </button>
    </div>
  </div>
</div>

<NotificationCenter
  open={showNotifications}
  onClose={() => (showNotifications = false)}
/>

<SettingsPanel
  open={showSettings}
  initialTab={settingsInitialTab}
  onClose={closeSettings}
/>

<style>
  .left-panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-elevated);
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .panel-header h2 {
    font-size: var(--font-size-xl);
    font-weight: 700;
    color: var(--accent-primary);
  }

  .new-chat-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    transition: all var(--transition-fast);
  }

  .new-chat-btn:active {
    background: var(--bg-hover);
    color: var(--accent-primary);
  }

  .panel-body {
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }

  .panel-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .footer-actions {
    display: flex;
    gap: var(--spacing-xs);
  }

  .footer-btn {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    transition: all var(--transition-fast);
  }

  .footer-btn:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .footer-btn.has-unread {
    color: var(--accent-primary);
  }

  .badge {
    position: absolute;
    top: 4px;
    right: 4px;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 700;
    line-height: 16px;
    text-align: center;
    color: white;
    background: var(--error);
    border-radius: 8px;
  }
</style>
