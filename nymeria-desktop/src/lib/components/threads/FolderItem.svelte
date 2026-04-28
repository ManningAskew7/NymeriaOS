<script lang="ts">
  import type { Thread, ThreadFolder } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import ThreadItem from './ThreadItem.svelte';

  interface Props {
    folder: ThreadFolder;
    threads: Thread[];
    currentThreadId: string | null;
    selectedIds: Set<string>;
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
  }

  let {
    folder,
    threads,
    currentThreadId,
    selectedIds,
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
  }: Props = $props();

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
</script>

<div class="folder-item">
  <div
    class="folder-header"
    role="button"
    tabindex="0"
    onclick={handleHeaderClick}
    onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleHeaderClick(); } }}
    oncontextmenu={handleContextMenu}
  >
    <span class="folder-chevron" class:collapsed={folder.collapsed}>
      <Icon name="chevronDown" size={14} />
    </span>
    <span class="folder-icon">
      <Icon name={folder.collapsed ? 'folder' : 'folderOpen'} size={16} />
    </span>
    {#if isEditingName}
      <!-- svelte-ignore a11y_autofocus -->
      <input
        type="text"
        class="folder-name-input"
        bind:value={editName}
        onkeydown={handleRenameKeydown}
        onblur={handleRenameBlur}
        onclick={(e) => e.stopPropagation()}
        autofocus
      />
    {:else}
      <span class="folder-name">{folder.name}</span>
    {/if}
    {#if isPinned}
      <span class="pin-indicator" title="Pinned"><Icon name="pin" size={12} /></span>
    {/if}
    <span class="folder-count">{threads.length}</span>
  </div>

  {#if !folder.collapsed}
    <div class="folder-contents">
      {#if threads.length === 0}
        <div class="empty-folder">Empty folder</div>
      {:else}
        {#each threads as thread (thread.id)}
          <ThreadItem
            {thread}
            isActive={thread.id === currentThreadId}
            isSelected={selectedIds.has(thread.id)}
            isPinned={isThreadPinned?.(thread.id) ?? false}
            taskCount={getThreadTaskCount(thread.id)}
            hasActiveTask={isThreadActive(thread.id)}
            isCallable={getCustomConfig(thread.id)?.callable ?? false}
            hasCustomConfig={getCustomConfig(thread.id)?.hasCustomizations}
            onSelect={(e) => onSelectThread(thread.id, e)}
            onDelete={() => onDeleteThread(thread.id)}
            onRename={(newTitle) => onRenameThread(thread.id, newTitle)}
            onConfigure={() => onConfigureThread(thread)}
            onOpenAgentConfig={onOpenAgentConfigThread ? () => onOpenAgentConfigThread(thread) : undefined}
            onExport={onExportThread ? () => onExportThread(thread) : undefined}
            onTogglePin={onTogglePinThread ? () => onTogglePinThread(thread.id) : undefined}
          />
        {/each}
      {/if}
    </div>
  {/if}
</div>

{#if contextMenu}
  <div class="context-backdrop" onclick={dismissContextMenu} onkeydown={(e) => e.key === 'Escape' && dismissContextMenu()} role="presentation" tabindex="-1"></div>
  <div class="context-menu" style="left: {contextMenu.x}px; top: {contextMenu.y}px;">
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
  }

  .folder-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-md);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast);
    user-select: none;
  }

  .folder-header:hover {
    background: var(--bg-hover);
  }

  .folder-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    transition: transform 200ms cubic-bezier(0.4, 0, 0.2, 1);
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
    box-shadow: 0 0 0 2px var(--accent-primary-alpha);
  }

  .pin-indicator {
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
    opacity: 0.6;
    flex-shrink: 0;
  }

  .folder-count {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 18px;
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
    background: var(--bg-elevated-2);
    color: var(--text-secondary);
    border-radius: var(--radius-full);
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
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 4px;
    min-width: 140px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
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
