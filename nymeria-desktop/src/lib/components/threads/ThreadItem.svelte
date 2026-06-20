<script lang="ts">
  import type { Thread } from '$lib/types';
  import { slide } from 'svelte/transition';
  import { focusOnMount } from '$lib/actions/focus';
  import { Icon, KebabMenu } from '$lib/components/common';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';

  interface Props {
    thread: Thread;
    isActive: boolean;
    isSelected?: boolean;
    /** True whenever a multi-select is in progress (any row selected). Keeps the
        left checkbox revealed on every row and hides the per-row actions. */
    selectionActive?: boolean;
    isPinned?: boolean;
    isCallable?: boolean;
    taskCount?: number;
    hasActiveTask?: boolean;
    hasCustomConfig?: boolean;
    hasUnread?: boolean;
    onSelect: (e: MouseEvent) => void;
    onDelete: () => void;
    onRename: (newTitle: string) => void;
    onConfigure?: () => void;
    onOpenAgentConfig?: () => void;
    onTogglePin?: () => void;
    onExport?: () => void;
    onToggleSelect?: () => void;
  }

  let { thread, isActive, isSelected = false, selectionActive = false, isPinned = false, isCallable = false, taskCount, hasActiveTask, hasCustomConfig, hasUnread = false, onSelect, onDelete, onRename, onConfigure, onOpenAgentConfig, onTogglePin, onExport, onToggleSelect }: Props = $props();

  let menuOpen = $state(false);
  let isEditing = $state(false);
  let editTitle = $state('');
  let contextMenu = $state<{ x: number; y: number } | null>(null);

  function handleClick(e: MouseEvent) {
    // Don't navigate if the click landed on the row's action controls, the
    // selection checkbox, or while editing.
    const target = e.target as HTMLElement;
    if (target.closest('.row-actions') || target.closest('.select-box') || isEditing) return;
    onSelect(e);
  }

  function handleKeydown(e: KeyboardEvent) {
    const target = e.target as HTMLElement;
    if (target.closest('.row-actions') || target.closest('.select-box')) return;
    if (e.key === 'Enter' && !isEditing) {
      onSelect(e as unknown as MouseEvent);
    }
  }

  // The left selection checkbox. Stops propagation so toggling selection never
  // also fires the row's navigate handler (mirrors handlePinClick).
  function handleSelectClick(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    onToggleSelect?.();
  }

  function startRename() {
    editTitle = thread.title;
    isEditing = true;
  }

  function cancelEditing() {
    isEditing = false;
    editTitle = '';
  }

  function saveEdit() {
    const trimmed = editTitle.trim();
    if (trimmed && trimmed !== thread.title) {
      onRename(trimmed);
    }
    isEditing = false;
    editTitle = '';
  }

  function handleEditKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveEdit();
    } else if (e.key === 'Escape') {
      cancelEditing();
    }
  }

  function handleEditBlur() {
    // Small delay to allow click events to fire first
    setTimeout(() => {
      if (isEditing) {
        saveEdit();
      }
    }, 150);
  }

  function handleContextMenu(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    contextMenu = { x: e.clientX, y: e.clientY };
  }

  function dismissContextMenu() {
    contextMenu = null;
  }

  function handleContextSelect() {
    contextMenu = null;
    onToggleSelect?.();
  }

  function handleContextPin() {
    contextMenu = null;
    onTogglePin?.();
  }

  function handleContextConfigure() {
    contextMenu = null;
    onConfigure?.();
  }

  function handlePinClick(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    onTogglePin?.();
  }

  function handleCopyId() {
    contextMenu = null;
    navigator.clipboard.writeText(thread.id);
  }

  function handleContextExport() {
    contextMenu = null;
    onExport?.();
  }

  function handleContextRename() {
    contextMenu = null;
    startRename();
  }

  function handleContextDelete() {
    contextMenu = null;
    onDelete();
  }

  // Items for the shared kebab menu. "Behavior settings" and "Agent settings"
  // open the thread-config panel on the matching tab (Behavior / Agent), so each
  // label names its destination and the two read as distinct by scope; both
  // appear only when their handler is wired. Rename is local; Delete routes to
  // the list's existing confirm modal.
  let kebabItems = $derived([
    ...(onConfigure ? [{ label: 'Behavior settings', icon: 'cog', onSelect: () => onConfigure?.() }] : []),
    ...(onOpenAgentConfig
      ? [{ label: 'Agent settings', icon: 'tool', onSelect: () => onOpenAgentConfig?.() }]
      : []),
    { label: 'Rename', icon: 'edit', onSelect: startRename },
    { label: 'Delete', icon: 'trash', onSelect: onDelete, destructive: true },
  ]);
</script>

<div
  class="thread-item"
  class:active={isActive}
  class:selected={isSelected}
  class:selecting={selectionActive}
  class:editing={isEditing}
  class:callable={isCallable}
  class:menuOpen={menuOpen}
  data-thread-id={thread.id}
  onclick={handleClick}
  oncontextmenu={handleContextMenu}
  onkeydown={handleKeydown}
  role="button"
  tabindex="0"
  aria-current={isActive ? 'page' : undefined}
>
  {#if onToggleSelect && !isEditing}
    <button
      class="select-box"
      class:checked={isSelected}
      type="button"
      role="checkbox"
      aria-checked={isSelected}
      aria-label="Select thread"
      onclick={handleSelectClick}
    >
      {#if isSelected}<Icon name="check" size={12} />{/if}
    </button>
  {/if}

  <div class="thread-icon">
    {#if thread.platform === 'discord'}
      <span class="platform-icon discord" data-tooltip="Discord">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/>
        </svg>
      </span>
    {:else if thread.platform === 'telegram'}
      <span class="platform-icon telegram" data-tooltip="Telegram">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>
        </svg>
      </span>
    {:else if thread.platform === 'slack'}
      <span class="platform-icon slack" data-tooltip="Slack">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zm10.122 2.521a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.268 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zm-2.523 10.122a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>
        </svg>
      </span>
    {:else if thread.platform === 'matrix'}
      <span class="platform-icon matrix" data-tooltip="Matrix">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'whatsapp'}
      <span class="platform-icon whatsapp" data-tooltip="WhatsApp">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'messenger'}
      <span class="platform-icon messenger" data-tooltip="Messenger">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'webex'}
      <span class="platform-icon webex" data-tooltip="Webex">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'mattermost'}
      <span class="platform-icon mattermost" data-tooltip="Mattermost">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'zulip'}
      <span class="platform-icon zulip" data-tooltip="Zulip">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'rocketchat'}
      <span class="platform-icon rocketchat" data-tooltip="Rocket.Chat">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'teams'}
      <span class="platform-icon teams" data-tooltip="Microsoft Teams">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'googlechat'}
      <span class="platform-icon googlechat" data-tooltip="Google Chat">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'line'}
      <span class="platform-icon line" data-tooltip="LINE">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'signal'}
      <span class="platform-icon signal" data-tooltip="Signal">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'twitch'}
      <span class="platform-icon twitch" data-tooltip="Twitch">
        <Icon name="chat" size={14} />
      </span>
    {:else if thread.platform === 'trigger'}
      <span class="platform-icon trigger" data-tooltip="Trigger">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
        </svg>
      </span>
    {:else}
      <Icon name="chat" size={14} />
    {/if}
  </div>

  <div class="thread-content">
    {#if isEditing}
      <input
        type="text"
        class="edit-input"
        bind:value={editTitle}
        onkeydown={handleEditKeydown}
        onblur={handleEditBlur}
        use:focusOnMount
      />
    {:else}
      <span class="thread-title">{thread.title}</span>
      {#if thread.preview}
        <span class="thread-preview">{thread.preview}</span>
      {/if}
    {/if}
  </div>

  {#if !isEditing}
    <div class="thread-badges">
      {#if isCallable && !onOpenAgentConfig}
        <span class="callable-indicator" data-tooltip="Callable">&lt;</span>
      {/if}
      {#if isPinned}
        <span class="pin-indicator" data-tooltip="Pinned"><Icon name="pin" size={12} /></span>
      {/if}
      {#if hasUnread}
        <span class="unread-dot" data-tooltip="New message from Nymeria"></span>
      {/if}
      {#if thread.recovered}
        <span class="recovered-indicator" data-tooltip="Recovered backend thread">
          <Icon name="warning" size={12} />
        </span>
      {/if}
      {#if hasActiveTask}
        <span class="active-indicator"><span class="badge-spinner"></span></span>
      {/if}
      {#if taskCount && taskCount > 0}
        <span class="task-badge">{taskCount}</span>
      {/if}
    </div>
  {/if}

  {#if !isEditing && !selectionActive}
    <div class="row-actions">
      {#if onTogglePin}
        <button
          class="pin-toggle"
          class:pinned={isPinned}
          onclick={handlePinClick}
          type="button"
          data-tooltip={isPinned ? 'Unpin thread' : 'Pin thread'}
          aria-label={isPinned ? 'Unpin thread' : 'Pin thread'}
          aria-pressed={isPinned}
        >
          <Icon name="pin" size={14} />
        </button>
      {/if}
      <span class="kebab-wrap">
        <KebabMenu items={kebabItems} ariaLabel="Thread actions" bind:open={menuOpen} />
      </span>
    </div>
  {/if}
</div>

{#if contextMenu}
  <div class="context-backdrop" onclick={dismissContextMenu} onkeydown={(e) => e.key === 'Escape' && dismissContextMenu()} role="presentation" tabindex="-1">
  </div>
  <div class="context-menu" style="left: {contextMenu.x}px; top: {contextMenu.y}px;" transition:slide={DROPDOWN_TRANSITION}>
    {#if onToggleSelect}
      <button class="context-item" onclick={handleContextSelect} type="button">
        <Icon name="check" size={14} />
        <span>{isSelected ? 'Deselect' : 'Select'}</span>
      </button>
    {/if}
    {#if onTogglePin}
      <button class="context-item" onclick={handleContextPin} type="button">
        <Icon name="pin" size={14} />
        <span>{isPinned ? 'Unpin' : 'Pin'}</span>
      </button>
    {/if}
    {#if onConfigure}
      <button class="context-item" onclick={handleContextConfigure} type="button">
        <Icon name="cog" size={14} />
        <span>Behavior settings</span>
      </button>
    {/if}
    {#if onExport}
      <button class="context-item" onclick={handleContextExport} type="button">
        <Icon name="download" size={14} />
        <span>Export</span>
      </button>
    {/if}
    <button class="context-item" onclick={handleCopyId} type="button">
      <Icon name="copy" size={14} />
      <span>Copy ID</span>
    </button>
    <button class="context-item" onclick={handleContextRename} type="button">
      <Icon name="edit" size={14} />
      <span>Rename</span>
    </button>
    <button class="context-item context-delete" onclick={handleContextDelete} type="button">
      <Icon name="trash" size={14} />
      <span>Delete</span>
    </button>
  </div>
{/if}

<style>
  .thread-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    text-align: left;
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), transform var(--transition-fast);
    position: relative;
    cursor: pointer;
    /* The row behaves like a button — never selectable text. Critical for
       drag-to-select: without this, click-and-drag would highlight text
       rather than extend the multi-select range. */
    user-select: none;
    /* Stagger fade-in matching ActivityItem — each thread row slides in
       from -8px on the X axis with a small per-row delay, so switching
       folders<->teams tabs (which re-mounts the list via {#key}) cascades
       the rows in instead of popping them all at once. */
    animation: staggerFadeIn var(--transition-slow) backwards;
  }
  /* Cap rule first so individual nth-child(1..20) values below override it
     for the first 20 rows. Anything beyond 20 lands at 0.6s and pops in
     together — still feels like a coherent ending to the cascade rather
     than a sudden batch at 0s. */
  .thread-item:nth-child(n+21) { animation-delay: 0.6s; }
  .thread-item:nth-child(1) { animation-delay: 0.03s; }
  .thread-item:nth-child(2) { animation-delay: 0.06s; }
  .thread-item:nth-child(3) { animation-delay: 0.09s; }
  .thread-item:nth-child(4) { animation-delay: 0.12s; }
  .thread-item:nth-child(5) { animation-delay: 0.15s; }
  .thread-item:nth-child(6) { animation-delay: 0.18s; }
  .thread-item:nth-child(7) { animation-delay: 0.21s; }
  .thread-item:nth-child(8) { animation-delay: 0.24s; }
  .thread-item:nth-child(9) { animation-delay: 0.27s; }
  .thread-item:nth-child(10) { animation-delay: 0.3s; }
  .thread-item:nth-child(11) { animation-delay: 0.33s; }
  .thread-item:nth-child(12) { animation-delay: 0.36s; }
  .thread-item:nth-child(13) { animation-delay: 0.39s; }
  .thread-item:nth-child(14) { animation-delay: 0.42s; }
  .thread-item:nth-child(15) { animation-delay: 0.45s; }
  .thread-item:nth-child(16) { animation-delay: 0.48s; }
  .thread-item:nth-child(17) { animation-delay: 0.51s; }
  .thread-item:nth-child(18) { animation-delay: 0.54s; }
  .thread-item:nth-child(19) { animation-delay: 0.57s; }
  .thread-item:nth-child(20) { animation-delay: 0.6s; }

  .thread-item::before {
    content: '';
    position: absolute;
    left: 0;
    top: 50%;
    width: 3px;
    height: 0;
    background: var(--accent-primary);
    border-radius: 0 2px 2px 0;
    transform: translateY(-50%);
    /* Not var(--transition-normal): that composite already carries `ease`,
       and a second easing makes the whole declaration invalid (the bar
       snapped instead of growing). Duration + curve spelled separately. */
    transition: height 250ms var(--ease-emphasis);
  }

  .thread-item.active::before {
    height: 60%;
  }

  .thread-item:hover {
    background: var(--bg-hover);
  }

  .thread-item.active {
    background: var(--bg-active);
    box-shadow: inset 0 0 0 1px rgba(var(--accent-primary-rgb), 0.08);
  }

  .thread-item.selected {
    background: color-mix(in srgb, var(--accent-primary) 12%, var(--bg-hover));
    outline: 1px solid color-mix(in srgb, var(--accent-primary) 40%, transparent);
  }

  /* Left selection checkbox. A rounded SQUARE (distinct from the round-ish
     green todo "done" control by accent fill + left position), filled with the
     accent when checked. Collapsed to zero width at rest and revealed on row
     hover/focus or whenever a selection is in progress (.selecting). The
     negative margin cancels the parent's flex `gap` while collapsed, so a
     hidden checkbox adds no phantom space before the icon; revealing it
     animates both the width and that margin back to their open values. Kept out
     of `display:none`/`visibility:hidden` so it stays in the tab order: a
     keyboard user can Tab to it (it reveals via :focus-within) to start a
     selection. NB: the collapsed margin is deliberately -1 * the row's flex
     `gap` (--spacing-sm) so they cancel exactly; if the row gap ever changes,
     update this margin to match or a small phantom offset returns. */
  .select-box {
    flex-shrink: 0;
    box-sizing: border-box;
    width: 0;
    height: 18px;
    margin-right: calc(-1 * var(--spacing-sm));
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1.5px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-on-accent);
    cursor: pointer;
    opacity: 0;
    overflow: hidden;
    transition: width var(--transition-fast), margin-right var(--transition-fast),
      opacity var(--transition-fast), background var(--transition-fast),
      border-color var(--transition-fast);
  }

  .thread-item:hover .select-box,
  .thread-item:focus-within .select-box,
  .thread-item.selecting .select-box {
    width: 18px;
    margin-right: 0;
    opacity: 1;
  }

  .select-box:hover {
    border-color: var(--accent-primary);
  }

  .select-box.checked {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .select-box:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  .thread-item:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .thread-icon {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    margin-top: 1px;
    color: var(--text-muted);
  }

  .thread-item.active .thread-icon {
    color: var(--accent-primary);
  }

  .platform-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .platform-icon.discord {
    color: #5865F2;
  }

  .platform-icon.telegram {
    color: #26A5E4;
  }

  .platform-icon.slack {
    color: #E01E5A;
  }

  .platform-icon.matrix {
    color: #0DBD8B;
  }

  .platform-icon.whatsapp {
    color: #25D366;
  }

  .platform-icon.messenger {
    color: #0084FF;
  }

  .platform-icon.webex {
    color: #00BCEB;
  }

  .platform-icon.mattermost {
    color: #0058CC;
  }

  .platform-icon.zulip {
    color: #6492FE;
  }

  .platform-icon.rocketchat {
    color: #F5455C;
  }

  .platform-icon.teams {
    color: #6264A7;
  }

  .platform-icon.googlechat {
    color: #1A73E8;
  }

  .platform-icon.line {
    color: #06C755;
  }

  .platform-icon.signal {
    color: #3A76F0;
  }

  .platform-icon.twitch {
    color: #9146FF;
  }

  .platform-icon.trigger {
    color: var(--accent-primary);
  }

  .thread-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .thread-title {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .thread-preview {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Pin + kebab float over the right edge as an absolute overlay, so revealing
     them never shifts or compresses the title the way the old in-flow icon
     cluster did. The resting status badges sit in the same spot and cross-fade
     out as these fade in. Hidden (and out of the tab order, via visibility)
     until the row is hovered or keyboard-focused, or while the kebab menu is
     open. */
  .row-actions {
    position: absolute;
    top: 50%;
    right: var(--spacing-md);
    transform: translateY(-50%);
    display: flex;
    align-items: center;
    gap: 2px;
    z-index: 1;
    opacity: 0;
    visibility: hidden;
    transition: opacity var(--transition-fast), visibility 0s var(--transition-fast);
  }

  .thread-item:hover .row-actions,
  .thread-item:focus-within .row-actions,
  .thread-item.menuOpen .row-actions {
    opacity: 1;
    visibility: visible;
    transition: opacity var(--transition-fast), visibility 0s 0s;
  }

  .pin-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: color var(--transition-fast), background var(--transition-fast),
      transform var(--transition-fast);
  }

  .pin-toggle:hover {
    color: var(--accent-primary);
    background: var(--bg-elevated-2);
  }

  .pin-toggle:active {
    transform: scale(var(--press-scale-icon));
  }

  /* Lit when pinned so the toggle reads as "on". */
  .pin-toggle.pinned {
    color: var(--accent-primary);
  }

  .kebab-wrap {
    display: inline-flex;
    align-items: center;
  }

  .thread-item.editing {
    background: var(--bg-active);
  }

  .edit-input {
    width: 100%;
    padding: 2px 4px;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    outline: none;
  }

  .edit-input:focus {
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .thread-badges {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
    margin-left: auto;
    padding-right: var(--spacing-xs);
    /* Cross-fade with the action overlay: the badges fade as pin+kebab fade in,
       keeping their flow space so the title width never changes. */
    transition: opacity var(--transition-fast);
  }

  .thread-item:hover .thread-badges,
  .thread-item:focus-within .thread-badges,
  .thread-item.menuOpen .thread-badges {
    opacity: 0;
  }

  .task-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border-radius: var(--radius-full);
  }

  .active-indicator {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
  }

  .badge-spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid var(--border-default);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: badgeSpin 0.8s linear infinite;
  }

  @keyframes badgeSpin {
    to { transform: rotate(360deg); }
  }

  .callable-indicator {
    display: inline-flex;
    align-items: center;
    font-size: var(--font-size-sm);
    font-weight: 700;
    color: var(--accent-primary);
    line-height: 1;
  }

  .pin-indicator {
    display: inline-flex;
    align-items: center;
    /* Rendered only when isPinned, so it must read as a state signal —
       not double-muted via opacity over already-muted text. */
    color: var(--text-muted);
  }

  .recovered-indicator {
    display: inline-flex;
    align-items: center;
    color: var(--warning);
    opacity: 0.9;
    flex-shrink: 0;
  }

  .unread-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent-primary);
    flex-shrink: 0;
    box-shadow: 0 0 6px color-mix(in srgb, var(--accent-primary) 60%, transparent);
  }

  .context-backdrop {
    position: fixed;
    inset: 0;
    z-index: 999;
  }

  .context-menu {
    position: fixed;
    z-index: 1000;
    background: var(--bg-elevated);
    border-radius: var(--radius-md);
    padding: 4px;
    min-width: 140px;
    /* §7 — floating context menu: shadow alone defines elevation;
       border would be redundant chrome. Tokenized to --shadow-md. */
    box-shadow: var(--shadow-md);
  }

  .context-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .context-item:hover {
    background: var(--bg-hover);
  }

  .context-delete:hover {
    color: var(--error);
  }
</style>
