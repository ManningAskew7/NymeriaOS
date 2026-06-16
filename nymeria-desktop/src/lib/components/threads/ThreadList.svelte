<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import { TAB_FADE } from '$lib/utils/transitions';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import ThreadItem from './ThreadItem.svelte';
  import FolderItem from './FolderItem.svelte';
  import ThreadSettingsPanel from './ThreadSettingsPanel.svelte';
  import { Icon, Modal, Button } from '$lib/components/common';
  import type { Thread, ThreadConfig, ThreadFolder, ThreadTeam, SortMode } from '$lib/types';

  let loadError = $state<string | null>(null);
  let configureThread = $state<Thread | null>(null);
  let configureInitialTab = $state<'behavior' | 'agent'>('behavior');
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

  // Ctrl-hover multi-select. After a chat is selected/active, the user can
  // hold Ctrl (Cmd on Mac) and move the cursor across other chat rows — every
  // row the cursor passes over gets added to the selection range anchored on
  // the active chat. Release Ctrl to commit. No clicking required during the
  // gesture.
  let ctrlHoverActive = false;
  let ctrlHoverAnchorId: string | null = null;
  let ctrlHoverInitialSelection: Set<string> | null = null;

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

  function toggleSelectThread(threadId: string) {
    const next = new Set(selectedIds);
    if (next.has(threadId)) {
      next.delete(threadId);
    } else {
      next.add(threadId);
    }
    selectedIds = next;
    lastClickedId = threadId;
  }

  // Arrow Up / Down cycles through the visible threads in the sidebar once a
  // thread is active. Guarded so it never hijacks normal arrow-key behavior
  // inside inputs, the message bar, or dialogs.
  async function handleArrowNav(e: KeyboardEvent) {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;

    const target = e.target as HTMLElement | null;
    if (target) {
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
      if (target.isContentEditable) return;
      if (target.closest('[contenteditable="true"], [contenteditable=""]')) return;
      if (target.closest('[role="dialog"]')) return;
    }

    const currentId = threadsStore.currentThreadId;
    if (!currentId) return;

    const ids = getVisibleThreadIds();
    if (ids.length === 0) return;

    const idx = ids.indexOf(currentId);
    if (idx === -1) return;

    const nextIdx = e.key === 'ArrowDown'
      ? Math.min(ids.length - 1, idx + 1)
      : Math.max(0, idx - 1);
    if (nextIdx === idx) {
      e.preventDefault();
      return;
    }

    const nextId = ids[nextIdx];
    const title = threadsStore.threads.find(t => t.id === nextId)?.title ?? nextId;
    e.preventDefault();
    await switchToThread(nextId, { ensureTitle: title });
  }

  /** Resolve which thread-row sits under a mouse point. */
  function threadIdAtPoint(x: number, y: number): string | null {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const row = (el as Element).closest('[data-thread-id]') as HTMLElement | null;
    return row?.dataset.threadId ?? null;
  }

  function handleCtrlKeyDown(e: KeyboardEvent) {
    if (e.key !== 'Control' && e.key !== 'Meta') return;
    if (ctrlHoverActive) return; // already active (key auto-repeat)
    // Need an anchor — the currently-active chat, or whatever was last clicked.
    const anchor = threadsStore.currentThreadId ?? lastClickedId;
    if (!anchor) return;
    // Arm the gesture, but DON'T touch the selection yet — that way a Ctrl
    // press for an unrelated shortcut (Ctrl+S, Ctrl+C, …) leaves the
    // selection alone. The anchor only joins the selection once the cursor
    // actually moves over a thread row in handleCtrlHoverMove below.
    ctrlHoverActive = true;
    ctrlHoverAnchorId = anchor;
    ctrlHoverInitialSelection = new Set(selectedIds);
  }

  function handleCtrlKeyUp(e: KeyboardEvent) {
    if (e.key !== 'Control' && e.key !== 'Meta') return;
    ctrlHoverActive = false;
    ctrlHoverAnchorId = null;
    ctrlHoverInitialSelection = null;
  }

  function handleCtrlHoverMove(e: MouseEvent) {
    if (!ctrlHoverActive || !ctrlHoverAnchorId) return;
    // Browsers don't repaint modifier state on mousemove without the key
    // event firing first, but if the user releases Ctrl elsewhere (alt-tab,
    // dropped focus), e.ctrlKey/e.metaKey will be false here — guard so we
    // don't keep extending the range after Ctrl is gone.
    if (!(e.ctrlKey || e.metaKey)) {
      handleCtrlKeyUp({ key: 'Control' } as KeyboardEvent);
      return;
    }

    const overId = threadIdAtPoint(e.clientX, e.clientY);
    if (!overId) return;

    const visible = getVisibleThreadIds();
    const startIdx = visible.indexOf(ctrlHoverAnchorId);
    const endIdx = visible.indexOf(overId);
    if (startIdx === -1 || endIdx === -1) return;

    const [lo, hi] = startIdx < endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
    const next = new Set(ctrlHoverInitialSelection ?? []);
    for (let i = lo; i <= hi; i++) next.add(visible[i]);
    selectedIds = next;
    lastClickedId = overId;
  }

  // Reset hover-select if the window loses focus (e.g. alt-tab) so the user
  // doesn't return to find a stale anchor still extending the selection.
  function handleWindowBlur() {
    if (!ctrlHoverActive) return;
    ctrlHoverActive = false;
    ctrlHoverAnchorId = null;
    ctrlHoverInitialSelection = null;
  }

  onMount(() => {
    // capture: true so we see the originally-focused element via e.target
    // before any default behavior moves focus.
    window.addEventListener('keydown', handleArrowNav, true);
    window.addEventListener('keydown', handleCtrlKeyDown);
    window.addEventListener('keyup', handleCtrlKeyUp);
    window.addEventListener('mousemove', handleCtrlHoverMove);
    window.addEventListener('blur', handleWindowBlur);
    return () => {
      window.removeEventListener('keydown', handleArrowNav, true);
      window.removeEventListener('keydown', handleCtrlKeyDown);
      window.removeEventListener('keyup', handleCtrlKeyUp);
      window.removeEventListener('mousemove', handleCtrlHoverMove);
      window.removeEventListener('blur', handleWindowBlur);
    };
  });

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
        loadError = 'Could not load threads. Check your connection and try again.';
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

  async function openThreadSettings(thread: Thread, initialTab: 'behavior' | 'agent') {
    configureInitialTab = initialTab;
    try {
      await threadConfigStore.loadConfig(thread.id);
    } catch {
      // Let the settings panel open with its empty fallback state; saving will surface API errors.
    }
    configureThread = thread;
  }

  function handleConfigureThread(thread: Thread) {
    void openThreadSettings(thread, 'behavior');
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
      loadError = humanizeErrorText(e, { action: 'export', resource: 'the thread' });
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
      loadError = humanizeErrorText(e, { action: 'import', resource: 'the thread' });
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
      loadError = humanizeErrorText(e, { action: 'create', resource: 'the team' });
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
      loadError = humanizeErrorText(e, { action: 'update', resource: 'the team' });
    }
  }

  async function handleRenameTeam(teamId: string, name: string) {
    try {
      await threadsStore.renameThreadTeam(teamId, name);
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'update', resource: 'the team name' });
    }
  }

  async function handleDeleteTeam(teamId: string) {
    try {
      await threadsStore.deleteThreadTeam(teamId);
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'delete', resource: 'the team' });
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
        aria-label="Show folders"
        aria-pressed={threadsStore.organizationMode === 'folders'}
        onclick={() => threadsStore.setOrganizationMode('folders')}
      >
        <Icon name="folder" size={14} />
      </button>
      <button
        class="mode-btn"
        class:active={threadsStore.organizationMode === 'teams'}
        type="button"
        title="Show teams"
        aria-label="Show teams"
        aria-pressed={threadsStore.organizationMode === 'teams'}
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
      aria-label="Import thread"
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
          aria-current={threadsStore.sortMode === opt.value ? 'true' : undefined}
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
      <button type="button" onclick={() => loadError = null}>Dismiss</button>
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
        <Icon name="chat" size={32} />
        <p>No threads yet</p>
        <p class="hint">Start a new thread to begin</p>
      </div>
    {:else}
    <!-- Tab content (folders <-> teams) crossfades via {#key} + the shared
         TAB_FADE recipe, matching the right sidebar's tab-switch effect. The
         wrapping .tab-content is a single-cell grid so the outgoing and
         incoming keyed panes occupy the same slot during the fade — they
         overlap instead of stacking vertically, which is what would otherwise
         cause the threads-container scrollbar to flicker. -->
    <div class="tab-content">
    {#key threadsStore.organizationMode}
    <div class="tab-pane" in:fly={TAB_FADE} out:fly={TAB_FADE}>
    {#if threadsStore.organizationMode === 'teams'}
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
                  onToggleSelect={() => toggleSelectThread(thread.id)}
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
              onToggleSelect={() => toggleSelectThread(thread.id)}
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
                  onToggleSelect={() => toggleSelectThread(thread.id)}
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
              onToggleSelect={() => toggleSelectThread(thread.id)}
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
    </div>
    {/key}
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
              placeholder="New folder name…"
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
              placeholder="New team name…"
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
        <button class="bulk-btn bulk-cancel" type="button" onclick={clearSelection} aria-label="Clear selection" data-tooltip="Clear selection">
          <Icon name="x" size={14} />
        </button>
        <div class="bulk-actions-row">
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
        </div>
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
    <Button variant="primary" onclick={() => (importWarnings = [])}>Close</Button>
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

  /* Single-cell grid: both keyed .tab-pane copies stack into the same cell
     during the fade, so the outgoing/incoming panes overlap instead of
     pushing each other vertically. Matches RightPanel's tab-switch effect. */
  .tab-content {
    display: grid;
    grid-template-columns: 1fr;
  }
  .tab-pane {
    grid-area: 1 / 1;
    min-width: 0;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
  }

  .empty-state p {
    margin: 0;
  }

  .empty-state .hint {
    font-size: var(--font-size-sm);
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
    border-radius: var(--radius-md);
    padding: 4px;
    min-width: 120px;
    /* §7 — floating dropdown: shadow alone defines elevation; border
       would be redundant chrome. Tokenized to --shadow-md. */
    box-shadow: var(--shadow-md);
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
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    padding: var(--spacing-sm) var(--spacing-md);
  }

  .bulk-actions-row {
    flex: 1 1 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .bulk-count {
    flex: 1 1 auto;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .bulk-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    white-space: nowrap;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .bulk-group {
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    border: 1px solid var(--accent-tint-border);
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
    margin-left: auto;
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
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .folder-picker-create-btn {
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-on-accent);
    background: var(--accent-primary);
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast), opacity var(--transition-fast);
  }

  .folder-picker-create-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Real hover (accent darken) rather than the prior opacity-only 0.9, so
     the press affordance follows the active theme's accent like every other
     primary control. */
  .folder-picker-create-btn:not(:disabled):hover {
    background: var(--accent-hover);
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
