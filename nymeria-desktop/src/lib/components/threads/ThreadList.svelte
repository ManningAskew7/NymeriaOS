<script lang="ts">
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { api } from '$lib/services/api.svelte';
  import ThreadItem from './ThreadItem.svelte';
  import FolderItem from './FolderItem.svelte';
  import ThreadSettingsPanel from './ThreadSettingsPanel.svelte';
  import { Icon, Modal, Button } from '$lib/components/common';
  import type { Thread, ThreadConfig, ThreadFolder, ThreadTeam, SortMode } from '$lib/types';

  let loadError = $state<string | null>(null);
  let configureThread = $state<Thread | null>(null);
  let configureInitialTab = $state<'instructions' | 'agent'>('instructions');
  let deleteConfirmThreadId = $state<string | null>(null);
  let deleteConfirmTitle = $state('');
  let importInput: HTMLInputElement | null = null;
  let importing = $state(false);
  let importReportTitle = $state('');
  let importWarnings = $state<string[]>([]);
  let retryingSync = $state(false);

  async function handleRetrySync() {
    retryingSync = true;
    try {
      await threadsStore.syncFromBackend();
    } finally {
      retryingSync = false;
    }
  }

  // Multi-select state
  let selectedIds = $state<Set<string>>(new Set());
  let lastClickedId = $state<string | null>(null);

  // Sort dropdown
  let showSortDropdown = $state(false);

  // Folder picker (for bulk group action)
  let showFolderPicker = $state(false);
  let showTeamPicker = $state(false);
  let newFolderName = $state('');
  let newTeamName = $state('');

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

  function pinFirst(a: Thread, b: Thread): number {
    return (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
  }

  function sortThreadsForMode(items: Thread[]): Thread[] {
    switch (threadsStore.sortMode) {
      case 'recent':
        return [...items].sort((a, b) => pinFirst(a, b) || b.updatedAt.getTime() - a.updatedAt.getTime());
      case 'oldest':
        return [...items].sort((a, b) => pinFirst(a, b) || a.createdAt.getTime() - b.createdAt.getTime());
      case 'alphabetical':
        return [...items].sort((a, b) => pinFirst(a, b) || a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
      case 'tasks':
        return [...items].sort((a, b) => pinFirst(a, b) || (threadsStore.getThreadTaskCount(b.id) - threadsStore.getThreadTaskCount(a.id)) || b.updatedAt.getTime() - a.updatedAt.getTime());
      case 'active':
        return [...items].sort((a, b) => {
          const p = pinFirst(a, b);
          if (p !== 0) return p;
          const aActive = threadsStore.isThreadActive(a.id) ? 1 : 0;
          const bActive = threadsStore.isThreadActive(b.id) ? 1 : 0;
          return bActive - aActive || b.updatedAt.getTime() - a.updatedAt.getTime();
        });
      default:
        return items;
    }
  }

  function groupThreadsByDate(items: Thread[]): { label: string; threads: Thread[] }[] {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const weekAgo = new Date(today);
    weekAgo.setDate(weekAgo.getDate() - 7);

    const pinnedGroup: { label: string; threads: Thread[] } = { label: 'Pinned', threads: [] };
    const groups: { label: string; threads: Thread[] }[] = [
      { label: 'Today', threads: [] },
      { label: 'Yesterday', threads: [] },
      { label: 'Previous 7 Days', threads: [] },
      { label: 'Older', threads: [] },
    ];

    for (const thread of [...items].sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime())) {
      if (thread.pinned) {
        pinnedGroup.threads.push(thread);
        continue;
      }
      const threadDate = new Date(thread.updatedAt.getFullYear(), thread.updatedAt.getMonth(), thread.updatedAt.getDate());
      if (threadDate >= today) groups[0].threads.push(thread);
      else if (threadDate >= yesterday) groups[1].threads.push(thread);
      else if (threadDate >= weekAgo) groups[2].threads.push(thread);
      else groups[3].threads.push(thread);
    }

    const result: { label: string; threads: Thread[] }[] = [];
    if (pinnedGroup.threads.length > 0) result.push(pinnedGroup);
    for (const group of groups) {
      if (group.threads.length > 0) result.push(group);
    }
    return result;
  }

  function getUnteamedThreads(): Thread[] {
    const teamed = new Set(threadsStore.threadTeams.flatMap((team) => team.threadIds));
    return threadsStore.threads.filter((thread) => !teamed.has(thread.id));
  }

  function teamAsFolder(team: ThreadTeam): ThreadFolder {
    return {
      id: team.id,
      name: team.name,
      createdAt: new Date(),
      order: 0,
      threadIds: team.threadIds,
      collapsed: team.collapsed,
    };
  }

  // Build ordered list of all visible thread IDs for Shift+Click range selection
  function getVisibleThreadIds(): string[] {
    const ids: string[] = [];
    const threadMap = new Map(threadsStore.threads.map(t => [t.id, t]));

    if (threadsStore.organizationMode === 'teams') {
      for (const team of threadsStore.threadTeams) {
        if (!team.collapsed) {
          const resolved = team.threadIds
            .map(tid => threadMap.get(tid))
            .filter((t): t is Thread => t !== undefined)
            .sort(pinFirst);
          for (const thread of resolved) ids.push(thread.id);
        }
      }
      const unteamed = getUnteamedThreads();
      const visibleUnteamed = threadsStore.sortMode === 'recent'
        ? groupThreadsByDate(unteamed).flatMap(group => group.threads)
        : sortThreadsForMode(unteamed);
      for (const thread of visibleUnteamed) ids.push(thread.id);
      return ids;
    }

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
      if (/\b401\b|\b403\b|unauthori[sz]ed|forbidden|not signed in/i.test(result.error)) {
        loadError = 'Your session is not authorized. Sign in again to load this thread.';
      } else if (/\b404\b/.test(result.error)) {
        loadError = 'That thread no longer exists on the backend.';
      } else {
        loadError = 'Could not load chat history. Check your connection and try again.';
      }
    }
  }

  function handleDeleteThread(threadId: string) {
    const thread = threadsStore.threads.find(t => t.id === threadId);
    deleteConfirmTitle = thread?.title || 'this thread';
    deleteConfirmThreadId = threadId;
  }

  function confirmDelete() {
    const threadId = deleteConfirmThreadId;
    if (!threadId) return;
    deleteConfirmThreadId = null;

    if (threadId === '__bulk__') {
      executeBulkDelete();
      return;
    }

    threadsStore.deleteThread(threadId);
    if (threadsStore.currentThreadId === threadId) {
      chatStore.clearMessages();
    }
    if (selectedIds.has(threadId)) {
      const next = new Set(selectedIds);
      next.delete(threadId);
      selectedIds = next;
    }
    api.deleteThreadConfig(threadId).catch(() => {});
  }

  function cancelDelete() {
    deleteConfirmThreadId = null;
  }

  function handleRenameThread(threadId: string, newTitle: string) {
    threadsStore.renameThread(threadId, newTitle);
  }

  async function openThreadSettings(thread: Thread, initialTab: 'instructions' | 'agent') {
    configureInitialTab = initialTab;
    try {
      await threadConfigStore.loadConfig(thread.id);
    } catch {
      // Let the settings panel open with its empty fallback state; saving will surface API errors.
    }
    configureThread = thread;
  }

  function handleConfigureThread(thread: Thread) {
    void openThreadSettings(thread, 'instructions');
  }

  function handleOpenAgentConfig(thread: Thread) {
    void openThreadSettings(thread, 'agent');
  }

  function isCallableThread(thread: Thread): boolean {
    const config = threadConfigStore.getConfig(thread.id);
    if (config) return config.callable;
    return (
      thread.callable === true ||
      (thread.callable === undefined && thread.platform === 'callable')
    );
  }

  function handleConfigSaved(config: ThreadConfig) {
    // Config is already in the store
  }

  function exportFilename(title: string): string {
    const slug = title
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 64);
    return `${slug || 'thread'}.nymeria-thread.json`;
  }

  function downloadJson(filename: string, data: unknown) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  async function handleExportThread(thread: Thread) {
    loadError = null;
    try {
      const document = await api.exportThread(thread.id);
      downloadJson(exportFilename(thread.title), document);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to export thread';
    }
  }

  function handleImportClick() {
    importInput?.click();
  }

  async function handleImportFile(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;

    importing = true;
    loadError = null;
    importReportTitle = '';
    importWarnings = [];
    try {
      const text = await file.text();
      const document = JSON.parse(text) as Record<string, unknown>;
      const result = await api.importThread(document);
      await threadsStore.syncFromBackend();
      await threadConfigStore.loadConfig(result.threadId).catch(() => {});
      await switchToThread(result.threadId);
      importReportTitle = result.title;
      importWarnings = result.warnings;
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to import thread';
    } finally {
      importing = false;
    }
  }

  // Sort controls
  function handleSortChange(mode: SortMode) {
    threadsStore.setSortMode(mode);
    showSortDropdown = false;
  }

  // Bulk actions
  function handleBulkDelete() {
    const count = selectedIds.size;
    deleteConfirmTitle = `${count} thread${count > 1 ? 's' : ''}`;
    deleteConfirmThreadId = '__bulk__';
  }

  function executeBulkDelete() {
    const needsClear = threadsStore.currentThreadId !== null && selectedIds.has(threadsStore.currentThreadId);
    for (const id of selectedIds) {
      threadsStore.deleteThread(id);
      api.deleteThreadConfig(id).catch(() => {});
    }
    if (needsClear) {
      chatStore.clearMessages();
    }
    selectedIds = new Set();
    lastClickedId = null;
  }

  function handleBulkGroup() {
    showFolderPicker = true;
    showTeamPicker = false;
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

  function handleBulkTeam() {
    showTeamPicker = true;
    showFolderPicker = false;
  }

  async function handleCreateTeamAndGroup() {
    const trimmed = newTeamName.trim();
    if (!trimmed) return;
    try {
      await threadsStore.createThreadTeam(trimmed, [...selectedIds]);
      newTeamName = '';
      showTeamPicker = false;
      selectedIds = new Set();
      lastClickedId = null;
      threadsStore.setOrganizationMode('teams');
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to create team';
    }
  }

  async function handleGroupIntoExistingTeam(teamId: string) {
    try {
      await threadsStore.addThreadsToTeam(teamId, [...selectedIds]);
      showTeamPicker = false;
      selectedIds = new Set();
      lastClickedId = null;
      threadsStore.setOrganizationMode('teams');
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to update team';
    }
  }

  async function handleRenameTeam(teamId: string, name: string) {
    try {
      await threadsStore.renameThreadTeam(teamId, name);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to rename team';
    }
  }

  async function handleDeleteTeam(teamId: string) {
    try {
      await threadsStore.deleteThreadTeam(teamId);
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to delete team';
    }
  }

  function clearSelection() {
    selectedIds = new Set();
    lastClickedId = null;
    showFolderPicker = false;
    showTeamPicker = false;
  }

  function handleNewFolderKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleCreateFolderAndGroup();
    } else if (e.key === 'Escape') {
      showFolderPicker = false;
    }
  }

  function handleNewTeamKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      void handleCreateTeamAndGroup();
    } else if (e.key === 'Escape') {
      showTeamPicker = false;
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
      showTeamPicker = false;
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
    <div class="organization-toggle" aria-label="Thread organization">
      <button
        class="mode-btn"
        class:active={threadsStore.organizationMode === 'folders'}
        type="button"
        title="Show folders"
        onclick={() => threadsStore.setOrganizationMode('folders')}
      >
        <Icon name="folder" size={14} />
      </button>
      <button
        class="mode-btn"
        class:active={threadsStore.organizationMode === 'teams'}
        type="button"
        title="Show teams"
        onclick={() => threadsStore.setOrganizationMode('teams')}
      >
        <Icon name="users" size={14} />
      </button>
    </div>
    <button
      class="import-trigger"
      type="button"
      onclick={handleImportClick}
      disabled={importing}
      title="Import thread"
    >
      <Icon name={importing ? 'loading' : 'upload'} size={14} />
      <span>Import</span>
    </button>
    <input
      bind:this={importInput}
      class="share-file-input"
      type="file"
      accept=".nymeria-thread.json,.json,application/json"
      onchange={handleImportFile}
    />
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

  {#if !threadsStore.initialSyncDone}
    <div class="sync-loading">
      <Icon name="loading" size={16} />
      <span>Loading threads…</span>
    </div>
  {:else}
    {#if threadsStore.lastSyncError}
      <div class="sync-error-banner">
        <p>Couldn't sync threads from backend. Showing cached data.</p>
        <button type="button" disabled={retryingSync} onclick={handleRetrySync}>
          {retryingSync ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    {/if}

    {#if threadsStore.threads.length === 0}
      <div class="empty-state">
        <p>No conversations yet</p>
        <p class="hint">Start a new chat to begin</p>
      </div>
    {:else if threadsStore.organizationMode === 'teams'}
      {#each threadsStore.threadTeams as team (team.id)}
        <FolderItem
          kind="team"
          folder={teamAsFolder(team)}
          threads={resolveFolderThreads(team.threadIds)}
          currentThreadId={threadsStore.currentThreadId}
          {selectedIds}
          isThreadPinned={(id) => threadsStore.isThreadPinned(id)}
          getThreadTaskCount={(id) => threadsStore.getThreadTaskCount(id)}
          isThreadActive={(id) => threadsStore.isThreadActive(id)}
          getCustomConfig={(id) => threadConfigStore.getConfig(id)}
          onSelectThread={handleThreadClick}
          onDeleteThread={handleDeleteThread}
          onRenameThread={handleRenameThread}
          onConfigureThread={handleConfigureThread}
          onOpenAgentConfigThread={handleOpenAgentConfig}
          onToggleCollapse={() => threadsStore.toggleTeamCollapse(team.id)}
          onRenameFolder={(name) => void handleRenameTeam(team.id, name)}
          onDeleteFolder={() => void handleDeleteTeam(team.id)}
          onTogglePinThread={(id) => threadsStore.togglePinThread(id)}
          onExportThread={handleExportThread}
        />
      {/each}

      {#if threadsStore.threadTeams.length > 0 && getUnteamedThreads().length > 0}
        <div class="folders-divider"></div>
      {/if}

      {#if threadsStore.sortMode === 'recent'}
        {#each groupThreadsByDate(getUnteamedThreads()) as group (group.label)}
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
                  isCallable={isCallableThread(thread)}
                  hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
                  hasUnread={thread.unread ?? false}
                  onSelect={(e) => handleThreadClick(thread.id, e)}
                  onDelete={() => handleDeleteThread(thread.id)}
                  onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
                  onConfigure={() => handleConfigureThread(thread)}
                  onOpenAgentConfig={() => handleOpenAgentConfig(thread)}
                  onTogglePin={() => threadsStore.togglePinThread(thread.id)}
                  onExport={() => handleExportThread(thread)}
                />
              {/each}
            </div>
          </div>
        {/each}
      {:else}
        <div class="group-threads">
          {#each sortThreadsForMode(getUnteamedThreads()) as thread (thread.id)}
            <ThreadItem
              {thread}
              isActive={thread.id === threadsStore.currentThreadId}
              isSelected={selectedIds.has(thread.id)}
              isPinned={thread.pinned ?? false}
              taskCount={threadsStore.getThreadTaskCount(thread.id)}
              hasActiveTask={threadsStore.isThreadActive(thread.id)}
              isCallable={isCallableThread(thread)}
              hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
              hasUnread={thread.unread ?? false}
              onSelect={(e) => handleThreadClick(thread.id, e)}
              onDelete={() => handleDeleteThread(thread.id)}
              onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
              onConfigure={() => handleConfigureThread(thread)}
              onOpenAgentConfig={() => handleOpenAgentConfig(thread)}
              onTogglePin={() => threadsStore.togglePinThread(thread.id)}
              onExport={() => handleExportThread(thread)}
            />
          {/each}
        </div>
      {/if}

      {#if getUnteamedThreads().length === 0 && threadsStore.threadTeams.length > 0}
        <div class="empty-state">
          <p class="hint">All threads are in teams</p>
        </div>
      {/if}
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
          onOpenAgentConfigThread={handleOpenAgentConfig}
          onToggleCollapse={() => threadsStore.toggleFolderCollapse(folder.id)}
          onRenameFolder={(name) => threadsStore.renameFolder(folder.id, name)}
          onDeleteFolder={() => threadsStore.deleteFolder(folder.id)}
          onTogglePin={() => threadsStore.togglePinFolder(folder.id)}
          onTogglePinThread={(id) => threadsStore.togglePinThread(id)}
          onExportThread={handleExportThread}
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
                  isCallable={isCallableThread(thread)}
                  hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
                  hasUnread={thread.unread ?? false}
                  onSelect={(e) => handleThreadClick(thread.id, e)}
                  onDelete={() => handleDeleteThread(thread.id)}
                  onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
                  onConfigure={() => handleConfigureThread(thread)}
                  onOpenAgentConfig={() => handleOpenAgentConfig(thread)}
                  onTogglePin={() => threadsStore.togglePinThread(thread.id)}
                  onExport={() => handleExportThread(thread)}
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
              isCallable={isCallableThread(thread)}
              hasCustomConfig={threadConfigStore.getConfig(thread.id)?.hasCustomizations}
              hasUnread={thread.unread ?? false}
              onSelect={(e) => handleThreadClick(thread.id, e)}
              onDelete={() => handleDeleteThread(thread.id)}
              onRename={(newTitle) => handleRenameThread(thread.id, newTitle)}
              onConfigure={() => handleConfigureThread(thread)}
              onOpenAgentConfig={() => handleOpenAgentConfig(thread)}
              onTogglePin={() => threadsStore.togglePinThread(thread.id)}
              onExport={() => handleExportThread(thread)}
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
      {#if showTeamPicker}
        <div class="folder-picker">
          <div class="folder-picker-header">Add to team</div>
          <div class="folder-picker-new">
            <input
              type="text"
              class="folder-picker-input"
              placeholder="New team name..."
              bind:value={newTeamName}
              onkeydown={handleNewTeamKeydown}
            />
            <button
              class="folder-picker-create-btn"
              type="button"
              disabled={!newTeamName.trim()}
              onclick={() => void handleCreateTeamAndGroup()}
            >Create</button>
          </div>
          {#if threadsStore.threadTeams.length > 0}
            <div class="folder-picker-existing">
              {#each threadsStore.threadTeams as team (team.id)}
                <button
                  class="folder-picker-option"
                  type="button"
                  onclick={() => void handleGroupIntoExistingTeam(team.id)}
                >
                  <Icon name="users" size={14} />
                  <span>{team.name}</span>
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
          Folder
        </button>
        <button class="bulk-btn bulk-group" type="button" onclick={handleBulkTeam}>
          <Icon name="users" size={14} />
          Team
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
    initialTab={configureInitialTab}
    onClose={() => (configureThread = null)}
    onSaved={handleConfigSaved}
  />
{/if}

<Modal
  title="Delete Thread"
  isOpen={deleteConfirmThreadId !== null}
  onClose={cancelDelete}
>
  <p class="delete-confirm-text">Are you sure you want to delete <strong>{deleteConfirmTitle}</strong>? This cannot be undone.</p>
  <div class="delete-confirm-actions">
    <Button variant="secondary" onclick={cancelDelete}>Cancel</Button>
    <Button variant="primary" onclick={confirmDelete}>Delete</Button>
  </div>
</Modal>

<Modal
  title="Thread Imported"
  isOpen={importWarnings.length > 0}
  onClose={() => (importWarnings = [])}
>
  <p class="import-report-text"><strong>{importReportTitle}</strong> was imported with configuration warnings.</p>
  <ul class="import-warning-list">
    {#each importWarnings as warning}
      <li>{warning}</li>
    {/each}
  </ul>
  <div class="delete-confirm-actions">
    <Button variant="primary" onclick={() => (importWarnings = [])}>Done</Button>
  </div>
</Modal>

<style>
  .delete-confirm-text {
    color: var(--text-secondary);
    margin: 0 0 var(--spacing-lg);
  }

  .delete-confirm-actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
  }

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

  .sync-loading {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-lg);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .sync-error-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    margin: var(--spacing-xs) var(--spacing-sm) var(--spacing-sm);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-md);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .sync-error-banner p {
    margin: 0;
    flex: 1;
  }

  .sync-error-banner button {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-primary);
    background: transparent;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast);
  }

  .sync-error-banner button:hover:not(:disabled) {
    background: var(--bg-hover);
  }

  .sync-error-banner button:disabled {
    opacity: 0.6;
    cursor: wait;
  }

  /* Sort bar */
  .sort-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: 0 var(--spacing-xs) var(--spacing-xs);
    position: relative;
  }

  .sort-trigger,
  .import-trigger,
  .mode-btn {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 0 8px;
    height: 28px;
    box-sizing: border-box;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .organization-toggle {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    height: 28px;
    box-sizing: border-box;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: transparent;
  }

  .mode-btn {
    width: 24px;
    height: 100%;
    justify-content: center;
    padding: 0;
    border: none;
  }

  .mode-btn.active {
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
  }

  .import-trigger {
    margin-left: auto;
  }

  .share-file-input {
    display: none;
  }

  .sort-trigger:hover,
  .import-trigger:hover:not(:disabled),
  .mode-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  .import-trigger:disabled {
    opacity: 0.6;
    cursor: wait;
  }

  .import-report-text {
    color: var(--text-secondary);
    margin: 0 0 var(--spacing-md);
  }

  .import-warning-list {
    margin: 0 0 var(--spacing-lg);
    padding-left: var(--spacing-lg);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
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
