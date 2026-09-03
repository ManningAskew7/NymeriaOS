<script lang="ts">
  import { onMount } from 'svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { teamedThreadIds, visibleThreadIds } from '$lib/utils/threadSections';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { switchToThread } from '$lib/stores/navigation.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import ThreadItem from './ThreadItem.svelte';
  import FolderItem from './FolderItem.svelte';
  import TeamSettingsModal from './TeamSettingsModal.svelte';
  import ThreadSettingsPanel from './ThreadSettingsPanel.svelte';
  import { Icon, Modal, Button } from '$lib/components/common';
  import type { Thread, ThreadConfig, SortMode } from '$lib/types';

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

  // Selection mode is implicit: it is "on" whenever at least one row is
  // selected. Rows use this to keep their left checkbox revealed and to hide
  // their per-row pin/kebab actions (the user acts on the whole set via the
  // bulk bar instead).
  let selectionActive = $derived(selectedIds.size > 0);

  // Polite screen-reader announcement of the running count (no global announce
  // helper exists; mirrors the ChatContainer sr-only/role=status pattern).
  let selectionAnnouncement = $derived(
    selectedIds.size > 0 ? `${selectedIds.size} thread${selectedIds.size > 1 ? 's' : ''} selected` : ''
  );

  // Bulk Pin is a smart toggle: if every selected thread is already pinned the
  // action unpins them all, otherwise it pins them all.
  let allSelectedPinned = $derived(
    selectedIds.size > 0 &&
    [...selectedIds].every((id) => threadsStore.threads.find((t) => t.id === id)?.pinned)
  );

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

  // Build ordered list of all visible thread IDs for Shift+Click range
  // selection: the rendered order, sections (skipping collapsed ones) then
  // the loose tail.
  function getVisibleThreadIds(): string[] {
    const loose =
      threadsStore.sortMode === 'recent'
        ? threadsStore.groupedUnfiledThreads.flatMap((group) => group.threads)
        : threadsStore.sortedUnfiledThreads;
    return visibleThreadIds(threadsStore.threadSections.sections, loose);
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

  // Escape clears the selection. The bulk bar's X is the visible exit; this is
  // an additive keyboard shortcut. Ignored while typing in a field so it never
  // competes with inline rename / the folder/team name inputs.
  function handleSelectionEscape(e: KeyboardEvent) {
    if (e.key !== 'Escape' || !selectionActive) return;
    const target = e.target as HTMLElement;
    if (target.closest('input, textarea, [contenteditable="true"]')) return;
    clearSelection();
  }

  onMount(() => {
    // capture: true so we see the originally-focused element via e.target
    // before any default behavior moves focus.
    window.addEventListener('keydown', handleArrowNav, true);
    window.addEventListener('keydown', handleCtrlKeyDown);
    window.addEventListener('keyup', handleCtrlKeyUp);
    window.addEventListener('keydown', handleSelectionEscape);
    window.addEventListener('mousemove', handleCtrlHoverMove);
    window.addEventListener('blur', handleWindowBlur);
    return () => {
      window.removeEventListener('keydown', handleArrowNav, true);
      window.removeEventListener('keydown', handleCtrlKeyDown);
      window.removeEventListener('keyup', handleCtrlKeyUp);
      window.removeEventListener('keydown', handleSelectionEscape);
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

    // Plain click always navigates and preserves any active selection. The
    // bulk bar's X (clearSelection) is the sole way to leave selection mode;
    // a row's checkbox toggles its own membership without opening it.
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

  // Pin (or unpin) the whole selection. Reuses the single-thread toggle, only
  // flipping rows that aren't already at the target state, so a mixed selection
  // ends up uniformly pinned. Non-destructive, so the selection is kept (unlike
  // Delete / group), letting the user see the result or act again.
  function handleBulkPin() {
    const target = !allSelectedPinned;
    for (const id of selectedIds) {
      const thread = threadsStore.threads.find((t) => t.id === id);
      if (thread && Boolean(thread.pinned) !== target) {
        threadsStore.togglePinThread(id);
      }
    }
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

  // A thread shows under its team, never also under a folder, so the Folder
  // action refuses a selection that includes teamed threads instead of
  // creating a membership the list would hide.
  let teamedSelection = $derived(teamedThreadIds(threadsStore.threadTeams, selectedIds));

  function handleBulkGroup() {
    showFolderPicker = true;
    showTeamPicker = false;
  }

  function handleCreateFolderAndGroup() {
    const trimmed = newFolderName.trim();
    if (!trimmed || teamedSelection.length > 0) return;
    const folder = threadsStore.createFolder(trimmed);
    threadsStore.addThreadsToFolder(folder.id, [...selectedIds]);
    newFolderName = '';
    showFolderPicker = false;
    selectedIds = new Set();
    lastClickedId = null;
  }

  function handleGroupIntoExisting(folderId: string) {
    if (teamedSelection.length > 0) return;
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

  // Team settings modal (rename/describe + shared team memory, #100 phase 3).
  let settingsTeamId = $state<string | null>(null);
  let settingsTeam = $derived(
    threadsStore.threadTeams.find((team) => team.id === settingsTeamId) ?? null
  );

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
  <!-- Polite, persistent live region: announces the running selection count.
       Always mounted (not inside the bulk bar) so even the first 0->1 change is
       announced, since screen readers only voice mutations to an existing
       region. -->
  <div class="sr-only" role="status" aria-live="polite" aria-atomic="true">{selectionAnnouncement}</div>

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
    <button
      class="import-trigger"
      type="button"
      onclick={handleImportClick}
      disabled={importing}
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
    <!-- One list: team and folder sections (pinned folders, teams, other
         folders; each thread under at most one of them, team first), then the
         loose threads. Rules and order: utils/threadSections.ts. -->
    {#each threadsStore.threadSections.sections as section (`${section.kind}:${section.group.id}`)}
      {#if section.kind === 'team'}
        <FolderItem
          kind="team"
          folder={section.group}
          threads={section.threads}
          currentThreadId={threadsStore.currentThreadId}
          {selectedIds}
          {selectionActive}
          isThreadPinned={(id) => threadsStore.isThreadPinned(id)}
          getThreadTaskCount={(id) => threadsStore.getThreadTaskCount(id)}
          isThreadActive={(id) => threadsStore.isThreadActive(id)}
          getCustomConfig={(id) => threadConfigStore.getConfig(id)}
          onSelectThread={handleThreadClick}
          onDeleteThread={handleDeleteThread}
          onRenameThread={handleRenameThread}
          onConfigureThread={handleConfigureThread}
          onOpenAgentConfigThread={handleOpenAgentConfig}
          onToggleCollapse={() => threadsStore.toggleTeamCollapse(section.group.id)}
          onRenameFolder={(name) => void handleRenameTeam(section.group.id, name)}
          onDeleteFolder={() => void handleDeleteTeam(section.group.id)}
          onOpenSettings={() => { settingsTeamId = section.group.id; }}
          onTogglePinThread={(id) => threadsStore.togglePinThread(id)}
          onToggleSelectThread={(id) => toggleSelectThread(id)}
          onExportThread={handleExportThread}
        />
      {:else}
        <FolderItem
          folder={section.group}
          threads={section.threads}
          currentThreadId={threadsStore.currentThreadId}
          {selectedIds}
          {selectionActive}
          isPinned={section.group.pinned ?? false}
          isThreadPinned={(id) => threadsStore.isThreadPinned(id)}
          getThreadTaskCount={(id) => threadsStore.getThreadTaskCount(id)}
          isThreadActive={(id) => threadsStore.isThreadActive(id)}
          getCustomConfig={(id) => threadConfigStore.getConfig(id)}
          onSelectThread={handleThreadClick}
          onDeleteThread={handleDeleteThread}
          onRenameThread={handleRenameThread}
          onConfigureThread={handleConfigureThread}
          onOpenAgentConfigThread={handleOpenAgentConfig}
          onToggleCollapse={() => threadsStore.toggleFolderCollapse(section.group.id)}
          onRenameFolder={(name) => threadsStore.renameFolder(section.group.id, name)}
          onDeleteFolder={() => threadsStore.deleteFolder(section.group.id)}
          onTogglePin={() => threadsStore.togglePinFolder(section.group.id)}
          onTogglePinThread={(id) => threadsStore.togglePinThread(id)}
          onToggleSelectThread={(id) => toggleSelectThread(id)}
          onExportThread={handleExportThread}
        />
      {/if}
    {/each}

    {#if threadsStore.threadSections.sections.length > 0 && threadsStore.unfiledThreads.length > 0}
      <div class="folders-divider"></div>
    {/if}

    <!-- Loose threads: in no team and no folder -->
    {#if threadsStore.sortMode === 'recent'}
      {#each threadsStore.groupedUnfiledThreads as group (group.label)}
        <div class="thread-group">
          <h3 class="group-label section-label">{group.label}</h3>
          <div class="group-threads">
            {#each group.threads as thread (thread.id)}
              <ThreadItem
                {thread}
                isActive={thread.id === threadsStore.currentThreadId}
                isSelected={selectedIds.has(thread.id)}
                {selectionActive}
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
            {selectionActive}
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

    {#if threadsStore.unfiledThreads.length === 0 && threadsStore.threadSections.sections.length > 0}
      <div class="empty-state">
        <p class="hint">Every thread is in a team or a folder</p>
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
          {#if teamedSelection.length > 0}
            <!-- Static explanation, not a live region: it is part of the
                 picker from the moment it opens. The controls stay focusable
                 (aria-disabled, not disabled) and point at it, so a keyboard
                 or screen-reader user hears the reason instead of finding an
                 empty picker; the click handlers refuse independently. -->
            <p class="folder-picker-note" id="folder-picker-note">
              {#if selectedIds.size === 1}
                This thread is in a team, which is where it is listed. Remove it from the team first to file it in a folder.
              {:else if teamedSelection.length === selectedIds.size}
                These threads are in a team, which is where they are listed. Remove them from the team first to file them in a folder.
              {:else if teamedSelection.length === 1}
                One of the selected threads is in a team, which is where it is listed. Deselect it, or remove it from the team, to file the rest in a folder.
              {:else}
                {teamedSelection.length} of the selected threads are in a team, which is where they are listed. Deselect them, or remove them from the team, to file the rest in a folder.
              {/if}
            </p>
          {/if}
          <div class="folder-picker-new">
            <input
              type="text"
              class="folder-picker-input"
              placeholder="New folder name…"
              bind:value={newFolderName}
              onkeydown={handleNewFolderKeydown}
              aria-disabled={teamedSelection.length > 0}
              aria-describedby={teamedSelection.length > 0 ? 'folder-picker-note' : undefined}
              readonly={teamedSelection.length > 0}
            />
            <button
              class="folder-picker-create-btn"
              type="button"
              disabled={!newFolderName.trim() || teamedSelection.length > 0}
              onclick={handleCreateFolderAndGroup}
            >Create</button>
          </div>
          {#if threadsStore.folders.length > 0}
            <div class="folder-picker-existing">
              {#each threadsStore.folders as folder (folder.id)}
                <button
                  class="folder-picker-option"
                  type="button"
                  aria-disabled={teamedSelection.length > 0}
                  aria-describedby={teamedSelection.length > 0 ? 'folder-picker-note' : undefined}
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
          <button class="bulk-btn bulk-group" type="button" onclick={handleBulkPin}>
            <Icon name="pin" size={14} />
            {allSelectedPinned ? 'Unpin' : 'Pin'}
          </button>
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

<TeamSettingsModal
  team={settingsTeam}
  isOpen={settingsTeam !== null}
  onClose={() => (settingsTeamId = null)}
/>

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
  /* Visually-hidden live region (WCAG clip pattern, matching ChatContainer). */
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

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
    /* Space ABOVE each date-group label (Yesterday, Previous 7 Days, …) is the
       previous row's 8px bottom padding + this margin + the label's 8px top
       padding. At 20px that lands the above-label gap at ~36px, double the
       ~18px gap below the label (and between chat rows) — "1a" below, "2× 1a"
       above. Follows the Appearance > Spacing setting; falls back to the tuned
       20px when no override is set. */
    margin-bottom: var(--ui-density-gap, 20px);
  }

  .group-label {
    /* Type role (uppercase / tracking / weight / muted) is the global .section-label. */
    /* Vertical padding is split (8px top, 10px bottom) so the whitespace below
       the label matches the gap between chat rows (~18px = each row's 8px
       padding twice + 2px flex gap): the first row adds its own 8px top
       padding, so 10px here lands the label-to-row gap at 18px. The gap above
       the label is set to the same 18px by the sort bar's bottom padding. */
    padding: var(--spacing-sm) var(--spacing-md) 10px;
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
    /* 11px bottom: sets the whitespace from the filter bar down to the first
       list item (a folder header or a section label) to ~20px once that item's
       own 8px top padding is added, matching the ~20px rhythm used elsewhere
       in the list. */
    padding: 0 var(--spacing-xs) 11px;
    position: relative;
  }

  .sort-trigger,
  .import-trigger {
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

  .import-trigger {
    margin-left: auto;
    /* Tertiary/ghost: drop the outline the sort chip carries so the toolbar
       hierarchy reads New Thread (primary) > sort (secondary, bordered) >
       Import (tertiary, borderless). The 1px transparent border is kept so
       Import stays vertically aligned with its bordered sibling. */
    border-color: transparent;
  }

  .share-file-input {
    display: none;
  }

  .sort-trigger:hover,
  .import-trigger:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  /* Keep Import borderless on hover too: the grouped hover rule above adds a
     visible border to the sort chip. */
  .import-trigger:hover:not(:disabled) {
    border-color: transparent;
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
    /* Asymmetric vertical margin. Top 10px: combined with the last folder
       row's own 8px bottom padding, the gap from that row's text down to the
       divider lands at ~18px (one "1a" row-gap), giving the folders area
       breathing room below its last item. Bottom 12px: the gap from the
       divider down to the first section label (e.g. PINNED) is ~20px (12px +
       the label's 8px top padding), matching the ~20px gap below the label and
       between chat rows. */
    margin: 10px var(--spacing-md) 12px;
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
    gap: var(--spacing-sm);
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

  /* Why the Folder action is refusing: teamed threads are listed under their
     team, so filing them would create a membership the list never shows. */
  .folder-picker-note {
    margin: 0 0 var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-left: 2px solid var(--accent-primary);
    font-size: var(--font-size-xs);
    line-height: 1.45;
    color: var(--text-secondary);
  }

  .folder-picker-input[aria-disabled='true'],
  .folder-picker-option[aria-disabled='true'] {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .folder-picker-option[aria-disabled='true']:hover {
    background: transparent;
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
