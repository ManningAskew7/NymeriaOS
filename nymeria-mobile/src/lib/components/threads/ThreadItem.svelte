<script lang="ts">
  import type { Thread } from '$lib/types';
  import { fade, fly } from 'svelte/transition';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    SHEET_RISE_IN,
    SHEET_RISE_OUT,
  } from '$lib/utils/transitions';
  import Icon from '$lib/components/common/Icon.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';

  interface Props {
    thread: Thread;
    isActive: boolean;
    onSelect: (id: string) => void;
    onDelete: (id: string) => void;
  }

  let { thread, isActive, onSelect, onDelete }: Props = $props();

  let showActions = $state(false);

  // Close the slide-up actions sheet on Escape — without this, keyboard users
  // (including those using a paired Bluetooth keyboard with a mobile device)
  // have no way out except backdrop click.
  $effect(() => {
    if (!showActions) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') showActions = false;
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  function formatTime(date: Date): string {
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const hours = diff / (1000 * 60 * 60);

    if (hours < 1) return 'Just now';
    if (hours < 24) return `${Math.floor(hours)}h ago`;

    const days = Math.floor(hours / 24);
    if (days === 1) return 'Yesterday';
    if (days < 7) return `${days}d ago`;

    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function getPlatformBadge(thread: Thread): string | null {
    switch (thread.platform) {
      case 'discord': return 'Discord';
      case 'telegram': return 'Telegram';
      case 'slack': return 'Slack';
      case 'matrix': return 'Matrix';
      case 'whatsapp': return 'WhatsApp';
      case 'messenger': return 'Messenger';
      case 'webex': return 'Webex';
      case 'mattermost': return 'Mattermost';
      case 'zulip': return 'Zulip';
      case 'rocketchat': return 'Rocket.Chat';
      case 'teams': return 'Teams';
      case 'googlechat': return 'Google Chat';
      case 'line': return 'LINE';
      case 'signal': return 'Signal';
      case 'twitch': return 'Twitch';
      case 'callable': return 'Agent';
      case 'trigger': return 'Trigger';
      default: return null;
    }
  }

  function handleContextMenu(e: MouseEvent) {
    e.preventDefault();
    showActions = !showActions;
  }

  function handleMoreClick(e: MouseEvent) {
    e.stopPropagation();
    showActions = !showActions;
  }

  function handleOverlayClick(e: MouseEvent) {
    e.stopPropagation();
    showActions = false;
  }

  function stopProp(e: MouseEvent) {
    e.stopPropagation();
  }

  function handleDelete(e: MouseEvent) {
    e.stopPropagation();
    showActions = false;
    onDelete(thread.id);
  }

  function handleRename(e: MouseEvent) {
    e.stopPropagation();
    showActions = false;
    const newTitle = prompt('Rename thread:', thread.title);
    if (newTitle?.trim()) {
      threadsStore.renameThread(thread.id, newTitle.trim());
    }
  }

  function handlePin(e: MouseEvent) {
    e.stopPropagation();
    showActions = false;
    threadsStore.togglePinThread(thread.id);
  }

  function handleCopyId(e: MouseEvent) {
    e.stopPropagation();
    showActions = false;
    navigator.clipboard.writeText(thread.id);
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
<div
  class="thread-item"
  class:active={isActive}
  class:pinned={thread.pinned}
  aria-current={isActive ? 'page' : undefined}
  onclick={() => onSelect(thread.id)}
  oncontextmenu={handleContextMenu}
>
  <div class="thread-content">
    <div class="thread-title-row">
      {#if thread.pinned}
        <Icon name="pin" size={12} class="pin-icon" />
      {/if}
      <span class="thread-title">{thread.title}</span>
    </div>
    <div class="thread-meta">
      {#if getPlatformBadge(thread)}
        <span class="platform-badge">{getPlatformBadge(thread)}</span>
      {/if}
      <span class="thread-time">{formatTime(thread.updatedAt)}</span>
    </div>
  </div>

  <button
    class="more-btn"
    onclick={handleMoreClick}
    title="Thread actions"
    aria-label="Thread actions"
  >
    <Icon name="chevronDown" size={16} />
  </button>

  {#if showActions}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="actions-overlay" onclick={handleOverlayClick} role="presentation" in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div class="actions-menu" onclick={stopProp} in:fly={SHEET_RISE_IN} out:fly={SHEET_RISE_OUT}>
        <button class="action-item" onclick={handleRename}>
          <Icon name="edit" size={18} />
          <span>Rename</span>
        </button>
        <button class="action-item" onclick={handlePin}>
          <Icon name="pin" size={18} />
          <span>{thread.pinned ? 'Unpin' : 'Pin'}</span>
        </button>
        <button class="action-item" onclick={handleCopyId}>
          <Icon name="copy" size={18} />
          <span>Copy ID</span>
        </button>
        <button class="action-item danger" onclick={handleDelete}>
          <Icon name="trash" size={18} />
          <span>Delete</span>
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .thread-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-md) var(--spacing-lg);
    min-height: var(--touch-target-min);
    cursor: pointer;
    transition: background var(--transition-fast);
    position: relative;
    border-bottom: 1px solid var(--border-subtle);
  }

  .thread-item:active {
    background: var(--bg-hover);
  }

  .thread-item.active {
    background: var(--bg-active);
    border-left: 3px solid var(--accent-primary);
  }

  .thread-content {
    flex: 1;
    min-width: 0;
  }

  .thread-title-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .thread-title {
    font-size: var(--font-size-base);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .thread-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: 2px;
  }

  .thread-time {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .platform-badge {
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    padding: 1px 6px;
    border-radius: var(--radius-sm);
  }

  .more-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: var(--radius-md);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .more-btn:active {
    background: var(--bg-hover);
  }

  :global(.pin-icon) {
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .actions-overlay {
    position: fixed;
    inset: 0;
    z-index: 100;
    background: rgba(0, 0, 0, 0.3);
    display: flex;
    align-items: flex-end;
    justify-content: center;
  }

  .actions-menu {
    width: 100%;
    max-width: 400px;
    background: var(--bg-elevated);
    border-top-left-radius: var(--radius-xl);
    border-top-right-radius: var(--radius-xl);
    padding: var(--spacing-md);
  }

  .action-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    width: 100%;
    padding: var(--spacing-md) var(--spacing-lg);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
    text-align: left;
  }

  .action-item:active {
    background: var(--bg-hover);
  }

  .action-item.danger {
    color: var(--error);
  }

</style>
