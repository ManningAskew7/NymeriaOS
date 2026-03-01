<script lang="ts">
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { api } from '$lib/services/api.svelte';
  import ThreadItem from './ThreadItem.svelte';
  import FolderItem from './FolderItem.svelte';
  import ThreadSettingsPanel from './ThreadSettingsPanel.svelte';
  import { Icon } from '$lib/components/common';
  import type { Thread, ThreadConfig, SortMode } from '$lib/types';

  let loadError = $state<string | null>(null);
  let configureThread = $state<Thread | null>(null);

  // Multi-select state
  let selectedIds = $state<Set<string>>(new Set());
  let lastClickedId = $state<string | null>(null);

  // Sort dropdown
  let showSortDropdown = $state(false);

  // Folder picker (for bulk group action)
  let showFolderPicker = $state(false);
  let newFolderName = $state('');

  const sortOptions: { value: SortMode; label: string }[] = [
    { value: 'recent',       label: 'Recent' },
    { value: 'oldest',       label: 'Oldest' },
    { value: 'alphabetical', label: 'A\u2013Z' },
    { value: 'tasks',        label: 'Most Tasks' },
    { value: 'active',       label: 'Active First' },
  ];

  function getCurrentSortLabel(): string {
    return sortOptions.find(o => o.value === threadsStore.sortMode)?.label ?? 'Recent';
  }

  // Build ordered list of all visible thread IDs for Shift+Click range selection
  function getVisibleThreadIds(): string[] {
    const ids: string[] = [];
    const threadMap = new Map(threadsStore.threads.map(t => [t.id, t]));

    // Folders first (pinned folders first, then by order)
    const sortedFolders = [...threadsStore.folders].sort((a, b) =>
      (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || a.order - b.order
    );
    for (const folder of sortedFolders) {
      if (!folder.collapsed) {
        // Match rendered order: pinned threads first within folder
        const resolved = folder.threadIds
          .map(tid => threadMap.get(tid))
          .filter((t): t is Thread => t !== undefined)
          .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
        for (const t of resolved) {
          ids.push(t.id);
        }
      }
    }

    // Then unfiled threads
    if (threadsStore.sortMode === 'recent') {
      for (const group of threadsStore.groupedUnfiledThreads) {
        for (const thread of group.threads) {
          ids.push(thread.id);
        }
      }
    } else {
      for (const thread of threadsStore.sortedUnfiledThreads) {
        ids.push(thread.id);
      }
    }

    return ids;
  }

  function handleThreadClick(threadId: string, event: MouseEvent) {
    const isCtrl = event.ctrlKey || event.metaKey;
    const isShift = event.shiftKey;

    if (isCtrl) {
      // Toggle individual selection
      const next = new Set(selectedIds);
      if (next.has(threadId)) {
        next.delete(threadId);
      } else {
        next.add(threadId);
      }
      selectedIds = next;
      lastClickedId = threadId;
      return;
    }

    if (isShift && lastClickedId) {
      // Range select
      const visible = getVisibleThreadIds();
      const startIdx = visible.indexOf(lastClickedId);
      const endIdx = visible.indexOf(threadId);
      if (startIdx !== -1 && endIdx !== -1) {
        const [lo, hi] = startIdx < endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
        const next = new Set(selectedIds);
        for (let i = lo; i <= hi; i++) {
          next.add(visible[i]);
        }
        selectedIds = next;
      }
      return;
    }

    // Plain click
    if (selectedIds.size > 0) {
      // Clear selection on plain click (don't navigate)
      selectedIds = new Set();
      lastClickedId = null;
      return;
    }

    // Normal navigation — set anchor for future Shift+Click
    lastClickedId = threadId;
    handleSelectThread(threadId);
  }

  async function handleSelectThread(threadId: string) {
    loadError = null;
    const result = await switchToThread(threadId);
    if (!result.success) {
      loadError = 'Could not load chat history. The thread may have been created before syncing was fixed.';
    }
  }

  function handleDeleteThread(threadId: string) {
    if (confirm('Are you sure you want to delete this thread?')) {
      threadsStore.deleteThread(threadId);
      if (threadsStore.currentThreadId === threadId) {
        chatStore.clearMessages();
      }
      // Remove from selection if selected
      if (selectedIds.has(threadId)) {
        const next = new Set(selectedIds);
        next.delete(threadId);
        selectedIds = next;
      }
      // Delete backend thread config (also removes callable thread tools via sync_agent_tools)
      api.deleteThreadConfig(threadId).catch(() => {});
    }
  }

  function handleRenameThread(threadId: string, newTitle: string) {
    threadsStore.renameThread(threadId, newTitle);
  }

  function handleConfigureThread(thread: Thread) {
    threadConfigStore.loadConfig(thread.id);
    configureThread = thread;
  }

  function handleConfigSaved(config: ThreadConfig) {
    // Config is already in the store
  }

  // Sort controls
  function handleSortChange(mode: SortMode) {
    threadsStore.setSortMode(mode);
    showSortDropdown = false;
  }

  // Bulk actions
  function handleBulkDelete() {
    const count = selectedIds.size;
    if (confirm(`Delete ${count} thread${count > 1 ? 's' : ''}? This cannot be undone.`)) {
      // Check if current thread is in selection before deleting (deleteThread changes currentThreadId)
      const needsClear = threadsStore.currentThreadId !== null && selectedIds.has(threadsStore.currentThreadId);
      for (const id of selectedIds) {
        threadsStore.deleteThread(id);
        // Delete backend thread config (also removes callable thread tools via sync_agent_tools)
        api.deleteThreadConfig(id).catch(() => {});
      }
      if (needsClear) {
        chatStore.clearMessages();
      }
      selectedIds = new Set();
      lastClickedId = null;
    }
  }

  function handleBulkGroup() {
    showFolderPicker = true;
  }

  function handleCreateFolderAndGroup() {
    const trimmed = newFolderName.trim();
    if (!trimmed) return;
    const folder = threadsStore.createFolder(trimmed);
    threadsStore.addThreadsToFolder(folder.id, [...selectedIds]);
    newFolderName = '';
    showFolderPicker = false;
    selectedIds = new Set();
    lastClickedId = null;
  }

  function handleGroupIntoExisting(folderId: string) {
    threadsStore.addThreadsToFolder(folderId, [...selectedIds]);
    showFolderPicker = false;
    selectedIds = new Set();
    lastClickedId = null;
  }

  function clearSelection() {
    selectedIds = new Set();
    lastClickedId = null;
    showFolderPicker = false;
  }

  function handleNewFolderKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleCreateFolderAndGroup();
    } else if (e.key === 'Escape') {
      showFolderPicker = false;
    }
  }

  // Resolve folder threads (filter out orphan IDs, pinned threads first)
  function resolveFolderThreads(threadIds: string[]): Thread[] {
    const threadMap = new Map(threadsStore.threads.map(t => [t.id, t]));
    const resolved = threadIds.map(id => threadMap.get(id)).filter((t): t is Thread => t !== undefined);
    return resolved.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
  }

  // Whether we have any threads at all (folders + unfiled)
  $effect(() => {
    // Close folder picker if selection is cleared
    if (selectedIds.size === 0) {
      showFolderPicker = false;
    }
  });
</script>

<div class="thread-list">
  <!-- Sort controls bar -->
  <div class="sort-bar">
    <button
      class="sort-trigger"
      type="button"
      onclick={() => (showSortDropdown = !showSortDropdown)}
    >
      <Icon name="sort" size={14} />
      <span>{getCurrentSortLabel()}</span>
      <Icon name="chevronDown" size={12} />
    </button>
  </div>

  {#if showSortDropdown}
    <div class="sort-backdrop" onclick={() => (showSortDropdown = false)} onkeydown={(e) => e.key === 'Escape' && (showSortDropdown = false)} role="presentation" tabindex="-1"></div>
    <div class="sort-dropdown">
      {#each sortOptions as opt}
        <button
          class="sort-option"
          class:active={threadsStore.sortMode === opt.value}
          type="button"
          onclick={() => handleSortChange(opt.value)}
        >
          {opt.label}
        </button>
      {/each}
    </div>
  {/if}

  {#if loadError}
    <div class="load-error">
      <p>{loadError}</p>
      <button onclick={() => loadError = null}>Dismiss</button>
    </div>
  {/if}

  {#if threadsStore.threads.length === 0}
    <div class="empty-state">
      <p>No conversations yet</p>
      <p class="hint">Start a new chat to begin</p>
    </div>
  {:else}
    <!-- Folders section -->
    {#each [...threadsStore.folders].sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || a.order - b.order) as folder (folder.id)}
      <FolderItem
        {folder}
        threads={resolveFolderThreads(folder.threadIds)}
        currentThreadId={threadsStore.currentThreadId}
        {selectedIds}
        isPinned={folder.pinned ?? false}
        isThreadPinned={(id) => threadsStore.isThreadPinned(id)}
        getThreadTaskCount={(id) => threadsStore.getThreadTaskCount(id)}
        isThreadActive={(id) => threadsStore.isThreadActive(id)}
        getCustomConfig={(id) => threadConfigStore.getConfig(id)}
        onSelectThread={handleThreadClick}
        onDeleteThread={handleDeleteThread}
        onRenameThread={handleRenameThread}
        onConfigureThread={handleConfigureThread}
        onToggleCollapse={() => threadsStore.toggleFolderCollapse(folder.id)}
        onRenameFolder={(name) => threadsStore.renameFolder(folder.id, name)}
        onDeleteFolder={() => threadsStore.deleteFolder(folder.id)}
        onTogglePin={() => threadsStore.togglePinFolder(folder.id)}
        onTogglePinThread={(id) => threadsStore.togglePinThread(id)}
      />
    {/each}

    {#if threadsStore.folders.length > 0 && threadsStore.unfiledThreads.length > 0}
      <div class="folders-divider"></div>
    {/if}

    <!-- Unfiled threads -->
    {#if threadsStore.sortMode === 'recent'}
      {#each threadsStore.groupedUnfiledThreads as group (group.label)}
        <div class="thread-group">
          <h3 class="group-label">{group.label}</h3>
          <div class="group-threads">
            {#each group.threads as thread (thread.id)}
              <ThreadItem
                {thread}
                isActive={thread.id === threadsStore.currentThreadId}
                isSelected={selectedIds.has(thread.id)}
                isPinned={thread.pinned ?? false}
                taskCount={threadsStore.getThreadTaskCount(thread.id)}
                hasActiveTask={threadsStore.isThreadActive(thread.id)}
                isCallable={threadConfigStore.isCallableThread(thread.id)}
                hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
                onSelect={(e) => handleThreadClick(thread.id, e)}
                onDelete={() => handleDeleteThread(thread.id)}
                onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
                onConfigure={() => handleConfigureThread(thread)}
                onTogglePin={() => threadsStore.togglePinThread(thread.id)}
              />
            {/each}
          </div>
        </div>
      {/each}
    {:else}
      <div class="group-threads">
        {#each threadsStore.sortedUnfiledThreads as thread (thread.id)}
          <ThreadItem
            {thread}
            isActive={thread.id === threadsStore.currentThreadId}
            isSelected={selectedIds.has(thread.id)}
            isPinned={thread.pinned ?? false}
            taskCount={threadsStore.getThreadTaskCount(thread.id)}
            hasActiveTask={threadsStore.isThreadActive(thread.id)}
            isCallable={threadConfigStore.isCallableThread(thread.id)}
            hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
            onSelect={(e) => handleThreadClick(thread.id, e)}
            onDelete={() => handleDeleteThread(thread.id)}
            onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
            onConfigure={() => handleConfigureThread(thread)}
            onTogglePin={() => threadsStore.togglePinThread(thread.id)}
          />
        {/each}
      </div>
    {/if}

    {#if threadsStore.unfiledThreads.length === 0 && threadsStore.folders.length > 0}
      <div class="empty-state">
        <p class="hint">All threads are in folders</p>
      </div>
    {/if}
  {/if}

  <!-- Bulk action bar -->
  {#if selectedIds.size > 0}
    <div class="bulk-action-bar">
      {#if showFolderPicker}
        <div class="folder-picker">
          <div class="folder-picker-header">Move to folder</div>
          <div class="folder-picker-new">
            <input
              type="text"
              class="folder-picker-input"
              placeholder="New folder name..."
              bind:value={newFolderName}
              onkeydown={handleNewFolderKeydown}
            />
            <button
              class="folder-picker-create-btn"
              type="button"
              disabled={!newFolderName.trim()}
              onclick={handleCreateFolderAndGroup}
            >Create</button>
          </div>
          {#if threadsStore.folders.length > 0}
            <div class="folder-picker-existing">
              {#each threadsStore.folders as folder (folder.id)}
                <button
                  class="folder-picker-option"
                  type="button"
                  onclick={() => handleGroupIntoExisting(folder.id)}
                >
                  <Icon name="folder" size={14} />
                  <span>{folder.name}</span>
                </button>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
      <div class="bulk-action-content">
        <span class="bulk-count">{selectedIds.size} selected</span>
        <button class="bulk-btn bulk-group" type="button" onclick={handleBulkGroup}>
          <Icon name="folder" size={14} />
          Group
        </button>
        <button class="bulk-btn bulk-delete" type="button" onclick={handleBulkDelete}>
          <Icon name="trash" size={14} />
          Delete
        </button>
        <button class="bulk-btn bulk-cancel" type="button" onclick={clearSelection}>
          <Icon name="x" size={14} />
        </button>
      </div>
    </div>
  {/if}
</div>

{#if configureThread}
  <ThreadSettingsPanel
    thread={configureThread}
    threadConfig={threadConfigStore.getConfig(configureThread.id) ?? null}
    onClose={() => (configureThread = null)}
    onSaved={handleConfigSaved}
  />
{/if}

<style>
  .thread-list {
    padding: var(--spacing-sm);
    position: relative;
  }

  .empty-state {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-sm);
    margin-top: var(--spacing-xs);
  }

  .thread-group {
    margin-bottom: var(--spacing-md);
  }

  .group-label {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: var(--spacing-sm) var(--spacing-md);
    margin: 0;
  }

  .group-threads {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .load-error {
    padding: var(--spacing-sm);
    margin: var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    font-size: var(--font-size-xs);
    color: var(--error);
  }

  .load-error p {
    margin: 0 0 var(--spacing-xs) 0;
  }

  .load-error button {
    font-size: var(--font-size-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px solid var(--error);
    border-radius: var(--radius-sm);
    color: var(--error);
    cursor: pointer;
  }

  .load-error button:hover {
    background: color-mix(in srgb, var(--error) 20%, transparent);
  }

  /* Sort bar */
  .sort-bar {
    display: flex;
    align-items: center;
    padding: 0 var(--spacing-xs) var(--spacing-xs);
    position: relative;
  }

  .sort-trigger {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .sort-trigger:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  .sort-backdrop {
    position: fixed;
    inset: 0;
    z-index: 998;
  }

  .sort-dropdown {
    position: absolute;
    top: 32px;
    left: var(--spacing-xs);
    z-index: 999;
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    padding: 4px;
    min-width: 120px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
  }

  .sort-option {
    display: block;
    width: 100%;
    text-align: left;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    cursor: pointer;
  }

  .sort-option:hover {
    background: var(--bg-hover);
  }

  .sort-option.active {
    color: var(--accent-primary);
    font-weight: 600;
  }

  /* Folders divider */
  .folders-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border-default), transparent);
    margin: var(--spacing-sm) var(--spacing-md);
  }

  /* Bulk action bar */
  .bulk-action-bar {
    position: sticky;
    bottom: 0;
    z-index: 10;
    display: flex;
    flex-direction: column;
    background: var(--glass-bg-strong);
    backdrop-filter: var(--glass-blur);
    -webkit-backdrop-filter: var(--glass-blur);
    border-top: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    margin-top: var(--spacing-sm);
    box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.2);
  }

  .bulk-action-content {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
  }

  .bulk-count {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
    margin-right: auto;
  }

  .bulk-btn {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .bulk-group {
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 30%, transparent);
  }

  .bulk-group:hover {
    background: color-mix(in srgb, var(--accent-primary) 20%, transparent);
  }

  .bulk-delete {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--error) 30%, transparent);
  }

  .bulk-delete:hover {
    background: color-mix(in srgb, var(--error) 20%, transparent);
  }

  .bulk-cancel {
    color: var(--text-muted);
    background: transparent;
    border: none;
    padding: 4px;
  }

  .bulk-cancel:hover {
    color: var(--text-primary);
  }

  /* Folder picker */
  .folder-picker {
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .folder-picker-header {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: var(--spacing-xs);
  }

  .folder-picker-new {
    display: flex;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-xs);
  }

  .folder-picker-input {
    flex: 1;
    padding: 4px 8px;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
  }

  .folder-picker-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha);
  }

  .folder-picker-create-btn {
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--bg-base);
    background: var(--accent-primary);
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: opacity var(--transition-fast);
  }

  .folder-picker-create-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .folder-picker-create-btn:not(:disabled):hover {
    opacity: 0.9;
  }

  .folder-picker-existing {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .folder-picker-option {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    text-align: left;
    padding: 4px 8px;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast);
  }

  .folder-picker-option:hover {
    background: var(--bg-hover);
  }
</style>
