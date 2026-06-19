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
  // Trigger element the dropdown anchors to. Only one bell renders at a time
  // (expanded vs collapsed footer), so a single ref bound in both branches is
  // correct.
  let bellButton = $state<HTMLButtonElement | undefined>(undefined);
  // Expanded footer only: the account chip the notifications popup bottom-aligns
  // to, so it shares a bottom baseline with the account menu (which opens from
  // this chip). Unset in the collapsed rail, where the popup falls back to the
  // bell.
  let footerAccountEl = $state<HTMLDivElement | undefined>(undefined);

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
        data-tooltip="New Thread"
        data-tooltip-pos="bottom"
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
        <div class="footer-account" bind:this={footerAccountEl}>
          <AccountBadge onOpenSettings={openSettings} />
        </div>
        <div class="footer-tools">
          <div class="notification-wrapper" bind:this={notificationWrapper}>
            <button
              bind:this={bellButton}
              class="footer-icon-btn"
              class:has-unread={notificationStore.unreadCount > 0}
              type="button"
              onclick={toggleNotifications}
              data-tooltip="Notifications"
              aria-label="Notifications"
              aria-haspopup="dialog"
              aria-expanded={showNotifications}
            >
              <Icon name="bell" size={16} />
              {#if notificationStore.unreadCount > 0}
                <span class="notification-badge-collapsed">{notificationStore.unreadCount}</span>
              {/if}
            </button>
            <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} anchorEl={bellButton} bottomAnchorEl={footerAccountEl} />
          </div>
          <button
            class="footer-icon-btn"
            type="button"
            onclick={() => openSettings()}
            data-tooltip="Settings"
            aria-label="Settings"
          >
            <Icon name="settings" size={16} />
          </button>
        </div>
      </div>
    {:else}
      <div class="notification-wrapper" bind:this={notificationWrapper}>
        <button
          bind:this={bellButton}
          class="icon-btn"
          class:has-unread={notificationStore.unreadCount > 0}
          type="button"
          onclick={toggleNotifications}
          data-tooltip="Notifications"
          aria-label="Notifications"
          aria-haspopup="dialog"
          aria-expanded={showNotifications}
        >
          <Icon name="bell" size={20} />
          {#if notificationStore.unreadCount > 0}
            <span class="notification-badge-collapsed">{notificationStore.unreadCount}</span>
          {/if}
        </button>
        <NotificationCenter isOpen={showNotifications} onClose={closeNotifications} anchorEl={bellButton} />
      </div>
      <AccountBadge onOpenSettings={openSettings} />
      <button
        class="icon-btn"
        type="button"
        onclick={() => openSettings()}
        data-tooltip="Settings"
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
    /* Bottom padding is halved (md -> sm) so the gap below the logo equals the
       gap above it. The space under the logo also picks up the thread list's
       own 8px top padding, so a full 16px bottom here would read as ~24px;
       8px + that 8px lands back at 16px, matching the 16px top padding. */
    padding: var(--spacing-md) var(--spacing-md) var(--spacing-sm);
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

  /* New Thread sits in the header's top-right corner, scaled to 0.9 (height
     and width reduced by the same 10%) so it reads a touch smaller than the
     component's default btn-sm. The scale lives on the WRAPPER, not the
     button: Button's own hover/press rules set transform on .btn (e.g.
     .btn-primary:hover applies translateY(-1px)) at higher specificity, so a
     transform on .btn would be wiped on hover and the pill would snap back to
     full size. Scaling the flex-item wrapper instead lets the button's
     hover-lift and press-scale compose inside a steady 0.9. transform-origin
     keeps it hugging the header's right padding as it shrinks. No translateY,
     so the button rests at its flex-centred position, 1px higher than the old
     optical nudge (per request). */
  .new-thread-wrap {
    transform: scale(0.9);
    transform-origin: right center;
  }

  .threads-container {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .sidebar-footer {
    /* Vertical padding split evenly (11px top and bottom) so the account row
       sits centred within the footer bar. The 22px total is unchanged from the
       earlier 6px-top / 16px-bottom split, so the footer's overall height (and
       therefore its top border line, kept level with the vertical middle of the
       prompt input bar) is preserved: only the row's position within the bar
       shifts down to centre. Both the footer and the input area are anchored to
       the window bottom, so this holds at any window height. */
    padding: 11px var(--spacing-md);
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
    transform: scale(var(--press-scale-icon));
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
    transform: scale(var(--press-scale-icon));
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
