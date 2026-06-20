<script lang="ts">
  import type { Thread, ThreadFolder } from '$lib/types';
  import { slide } from 'svelte/transition';
  import { focusOnMount } from '$lib/actions/focus';
  import { Icon } from '$lib/components/common';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import ThreadItem from './ThreadItem.svelte';

  interface Props {
    folder: ThreadFolder;
    threads: Thread[];
    kind?: 'folder' | 'team';
    currentThreadId: string | null;
    selectedIds: Set<string>;
    /** Mirrors ThreadList: true while a multi-select is in progress. */
    selectionActive?: boolean;
    isPinned?: boolean;
    isThreadPinned?: (id: string) => boolean;
    getThreadTaskCount: (id: string) => number;
    isThreadActive: (id: string) => boolean;
    getCustomConfig: (id: string) => { hasCustomizations?: boolean; callable?: boolean } | null | undefined;
    onSelectThread: (id: string, e: MouseEvent) => void;
    onDeleteThread: (id: string) => void;
    onRenameThread: (id: string, title: string) => void;
    onConfigureThread: (thread: Thread) => void;
    onOpenAgentConfigThread?: (thread: Thread) => void;
    onExportThread?: (thread: Thread) => void;
    onToggleCollapse: () => void;
    onRenameFolder: (name: string) => void;
    onDeleteFolder: () => void;
    onTogglePin?: () => void;
    onTogglePinThread?: (id: string) => void;
    onToggleSelectThread?: (id: string) => void;
  }

  let {
    folder,
    threads,
    kind = 'folder',
    currentThreadId,
    selectedIds,
    selectionActive = false,
    isPinned = false,
    isThreadPinned,
    getThreadTaskCount,
    isThreadActive,
    getCustomConfig,
    onSelectThread,
    onDeleteThread,
    onRenameThread,
    onConfigureThread,
    onOpenAgentConfigThread,
    onExportThread,
    onToggleCollapse,
    onRenameFolder,
    onDeleteFolder,
    onTogglePin,
    onTogglePinThread,
    onToggleSelectThread,
  }: Props = $props();

  const iconName = $derived(kind === 'team' ? 'users' : (folder.collapsed ? 'folder' : 'folderOpen'));

  let isEditingName = $state(false);
  let editName = $state('');
  let contextMenu = $state<{ x: number; y: number } | null>(null);

  function handleHeaderClick() {
    onToggleCollapse();
  }

  function handleContextMenu(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    contextMenu = { x: e.clientX, y: e.clientY };
  }

  function dismissContextMenu() {
    contextMenu = null;
  }

  function handleContextPin() {
    contextMenu = null;
    onTogglePin?.();
  }

  function handleContextRename() {
    contextMenu = null;
    editName = folder.name;
    isEditingName = true;
  }

  function handleContextDelete() {
    contextMenu = null;
    onDeleteFolder();
  }

  function saveRename() {
    const trimmed = editName.trim();
    if (trimmed && trimmed !== folder.name) {
      onRenameFolder(trimmed);
    }
    isEditingName = false;
    editName = '';
  }

  function cancelRename() {
    isEditingName = false;
    editName = '';
  }

  function handleRenameKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveRename();
    } else if (e.key === 'Escape') {
      cancelRename();
    }
  }

  function handleRenameBlur() {
    setTimeout(() => {
      if (isEditingName) saveRename();
    }, 150);
  }

  function isCallableThread(thread: Thread): boolean {
    const config = getCustomConfig(thread.id);
    if (config) return config.callable ?? false;
    return (
      thread.callable === true ||
      (thread.callable === undefined && thread.platform === 'callable')
    );
  }
</script>

<div class="folder-item">
  <div
    class="folder-header"
    role="button"
    tabindex="0"
    onclick={handleHeaderClick}
    onkeydown={(e) => {
      // Don't hijack keys destined for the inline rename input (Space would
      // otherwise be swallowed and toggle the folder instead of typing).
      if ((e.target as HTMLElement).closest('input')) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleHeaderClick(); }
    }}
    oncontextmenu={handleContextMenu}
  >
    <span class="folder-chevron" class:collapsed={folder.collapsed}>
      <Icon name="chevronDown" size={14} />
    </span>
    <span class="folder-icon">
      <Icon name={iconName} size={16} />
    </span>
    {#if isEditingName}
      <input
        type="text"
        class="folder-name-input"
        bind:value={editName}
        onkeydown={handleRenameKeydown}
        onblur={handleRenameBlur}
        onclick={(e) => e.stopPropagation()}
        use:focusOnMount
      />
    {:else}
      <span class="folder-name">{folder.name}</span>
    {/if}
    <span class="folder-count">{threads.length}</span>
    {#if isPinned}
      <span class="pin-indicator" data-tooltip="Pinned"><Icon name="pin" size={12} /></span>
    {/if}
  </div>

  {#if !folder.collapsed}
    <div class="folder-contents" transition:slide={DROPDOWN_TRANSITION}>
      {#if threads.length === 0}
        <div class="empty-folder">Empty folder</div>
      {:else}
        {#each threads as thread (thread.id)}
          <ThreadItem
            {thread}
            isActive={thread.id === currentThreadId}
            isSelected={selectedIds.has(thread.id)}
            {selectionActive}
            isPinned={isThreadPinned?.(thread.id) ?? false}
            taskCount={getThreadTaskCount(thread.id)}
            hasActiveTask={isThreadActive(thread.id)}
            isCallable={isCallableThread(thread)}
            hasCustomConfig={getCustomConfig(thread.id)?.hasCustomizations}
            hasUnread={thread.unread ?? false}
            onSelect={(e) => onSelectThread(thread.id, e)}
            onDelete={() => onDeleteThread(thread.id)}
            onRename={(newTitle) => onRenameThread(thread.id, newTitle)}
            onConfigure={() => onConfigureThread(thread)}
            onOpenAgentConfig={onOpenAgentConfigThread ? () => onOpenAgentConfigThread(thread) : undefined}
            onExport={onExportThread ? () => onExportThread(thread) : undefined}
            onTogglePin={onTogglePinThread ? () => onTogglePinThread(thread.id) : undefined}
            onToggleSelect={onToggleSelectThread ? () => onToggleSelectThread(thread.id) : undefined}
          />
        {/each}
      {/if}
    </div>
  {/if}
</div>

{#if contextMenu}
  <div class="context-backdrop" onclick={dismissContextMenu} onkeydown={(e) => e.key === 'Escape' && dismissContextMenu()} role="presentation" tabindex="-1"></div>
  <div class="context-menu" style="left: {contextMenu.x}px; top: {contextMenu.y}px;" transition:slide={DROPDOWN_TRANSITION}>
    {#if onTogglePin}
      <button class="context-item" onclick={handleContextPin} type="button">
        <Icon name="pin" size={14} />
        <span>{isPinned ? 'Unpin' : 'Pin'}</span>
      </button>
    {/if}
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
  .folder-item {
    margin-bottom: 2px;
    /* Stagger fade-in matching ActivityItem + ThreadItem — folders also
       cascade in from -8px on the X axis when the folders/teams tab
       re-mounts via {#key}. */
    animation: staggerFadeIn var(--transition-slow) backwards;
  }
  .folder-item:nth-child(1) { animation-delay: 0.03s; }
  .folder-item:nth-child(2) { animation-delay: 0.06s; }
  .folder-item:nth-child(3) { animation-delay: 0.09s; }
  .folder-item:nth-child(4) { animation-delay: 0.12s; }
  .folder-item:nth-child(5) { animation-delay: 0.15s; }
  .folder-item:nth-child(6) { animation-delay: 0.18s; }
  .folder-item:nth-child(7) { animation-delay: 0.21s; }
  .folder-item:nth-child(8) { animation-delay: 0.24s; }

  .folder-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast);
    user-select: none;
  }

  .folder-header:hover {
    background: var(--bg-hover);
  }

  /* Inset ring to match the sidebar list-row treatment (ThreadItem): the
     header sits flush in the scrolling thread list where the app.css outset
     baseline (+2px) is clip-prone. */
  .folder-header:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .folder-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
    flex-shrink: 0;
  }

  .folder-chevron.collapsed {
    transform: rotate(-90deg);
  }

  .folder-icon {
    display: flex;
    align-items: center;
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .folder-name {
    flex: 1;
    min-width: 0;
    /* Extra 4px (on top of the parent's 4px gap) so the icon-to-text gap matches
       the spacing-sm gap used by ThreadItem rows below. */
    margin-left: 4px;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .folder-name-input {
    flex: 1;
    min-width: 0;
    margin-left: 4px;
    padding: 1px 4px;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    outline: none;
  }

  .folder-name-input:focus {
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .pin-indicator {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    /* Rendered only when isPinned, so it must read as a state signal —
       not double-muted via opacity over already-muted text. */
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .folder-count {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    padding: 0;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-muted);
  }

  .folder-contents {
    padding-left: var(--spacing-md);
  }

  .empty-folder {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-style: italic;
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
