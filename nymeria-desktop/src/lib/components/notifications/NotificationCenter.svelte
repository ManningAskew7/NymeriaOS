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
    anchorEl?: HTMLElement;
    bottomAnchorEl?: HTMLElement;
  }

  let { isOpen, onClose, anchorEl, bottomAnchorEl }: Props = $props();

  // Fixed-position placement computed from the trigger's on-screen rect. The
  // dropdown lives inside the sidebar's `overflow: hidden` column, so absolute
  // positioning clipped it at the sidebar's right edge (and the chat painted
  // over the spill). position: fixed escapes that clip: no ancestor sets
  // transform/filter/contain, so the viewport is the containing block. The
  // element stays a DOM child of the trigger wrapper, so the outside-click
  // guard in Sidebar still recognises clicks inside it.
  const EDGE_PAD = 8; // keep clear of the window edge
  const TRIGGER_GAP = 8; // gap between the trigger and the dropdown
  // Matches AccountMenu's margin-bottom (--spacing-xs). When a bottom anchor is
  // given, the popup's bottom edge lands on the same baseline the account menu
  // opens from, so the two footer popups line up.
  const ACCOUNT_MENU_GAP = 4;
  const MAX_WIDTH = 280;

  let posLeft = $state(0);
  let posBottom = $state(0);
  let posWidth = $state(MAX_WIDTH);

  function reposition() {
    if (!anchorEl || typeof window === 'undefined') return;
    const r = anchorEl.getBoundingClientRect();
    const width = Math.min(MAX_WIDTH, window.innerWidth - EDGE_PAD * 2);
    // Center the dropdown horizontally over the bell, opening upward above it.
    // Clamp into the viewport so a near-edge trigger or collapsed rail can't
    // push it off-screen.
    const triggerCenter = r.left + r.width / 2;
    let left = triggerCenter - width / 2;
    left = Math.max(EDGE_PAD, Math.min(window.innerWidth - width - EDGE_PAD, left));
    posWidth = width;
    posLeft = left;
    // Vertical: with a bottom anchor (the footer account chip), align the
    // popup's bottom edge to the account menu's baseline (chip top, less the
    // --spacing-xs gap) so the two footer popups line up. Without one (the
    // collapsed rail), sit just above the bell itself.
    const bottomRef = bottomAnchorEl?.getBoundingClientRect();
    posBottom = bottomRef
      ? Math.max(EDGE_PAD, window.innerHeight - bottomRef.top + ACCOUNT_MENU_GAP)
      : Math.max(EDGE_PAD, window.innerHeight - r.top + TRIGGER_GAP);
  }

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

  // Position the dropdown when it opens, and keep it anchored if the window
  // resizes while it is open. The app shell never page-scrolls (fixed 100vh),
  // so resize is the only reflow that can move the trigger out from under it.
  $effect(() => {
    if (!isOpen) return;
    reposition();
    const onResize = () => reposition();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  });

  // §6 a11y — Escape closes the dropdown while open. Window listener so
  // the handler fires no matter where focus currently sits inside the
  // notification center (or outside it).
  $effect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });
</script>

{#if isOpen}
  <div
    class="notification-center"
    role="dialog"
    aria-label="Notifications"
    style:left="{posLeft}px"
    style:bottom="{posBottom}px"
    style:width="{posWidth}px"
    transition:slide={DROPDOWN_TRANSITION}
  >
    {#if notificationStore.notifications.length > 0}
      <div class="notification-header">
        <div class="header-actions">
          {#if notificationStore.unreadCount > 0}
            <button class="header-btn" type="button" onclick={handleMarkAllRead}>
              Mark all read
            </button>
          {/if}
          <button
            class="header-btn subtle"
            type="button"
            onclick={handleClearAll}
            data-tooltip="Delete all notifications"
          >
            Clear
          </button>
        </div>
      </div>
    {/if}

    <div class="notification-list">
      {#if notificationStore.loading && notificationStore.notifications.length === 0}
        <div class="notification-empty">
          <Icon name="loading" size={24} />
          <span>Loading notifications…</span>
        </div>
      {:else if notificationStore.notifications.length === 0}
        <div class="notification-empty">
          <!-- A crescent moon reads as "all quiet / nothing pending", a calmer
               empty state than the bell (which is the trigger icon). Greyed a
               touch below the text via .empty-moon. -->
          <Icon name="moon" size={20} class="empty-moon" />
          <span>No notifications.</span>
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
    /* Fixed so the dropdown escapes the sidebar's `overflow: hidden` and
       paints above the chat panel. Exact left/bottom/width come from inline
       styles computed against the trigger's rect (see reposition()). */
    position: fixed;
    background: var(--bg-base);
    border-radius: var(--radius-lg);
    /* §7 — floating popover: shadow alone defines elevation; border
       would be redundant chrome. */
    box-shadow: var(--shadow-lg);
    max-height: min(400px, calc(100vh - 96px));
    display: flex;
    flex-direction: column;
    z-index: 999;
  }

  .notification-header {
    /* Title removed (context already makes clear this popover is
       notifications); this row now only carries the Mark all read / Clear
       actions, right-aligned, and renders solely when notifications exist.
       No bottom border, so the list sits directly under the rounded top. */
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding: var(--spacing-sm) var(--spacing-md);
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
    gap: var(--spacing-md);
    padding: var(--spacing-xl);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  /* The moon sits a step quieter than the "No notifications" text: same hue
     (inherits the muted text color) but knocked back with opacity so it reads
     as a soft, restful mark rather than a label. */
  .notification-empty :global(.empty-moon) {
    opacity: 0.6;
  }
</style>
