import type { Thread, ThreadPlatform, ThreadFolder, ThreadTeam, ThreadTeamApi, SortMode } from '$lib/types';
import { api } from '$lib/services/api.svelte';
import { debugLog } from '$lib/utils/debug';
import { generateId } from '$lib/utils/ids';
import { detectThreadPlatform, isPlatformNativeThreadId } from '$lib/utils/platform';
import { bucketThreadsByDate, groupThreadsWithPinned, type ThreadDateGroup } from '$lib/utils/threadGrouping';
import { buildThreadSections, type ThreadSections } from '$lib/utils/threadSections';
import { scopedKey, registerIdentityReloadHook } from './config.svelte';

// localStorage keys are namespaced by the currently-connected user's id
// (resolved from GET /me). Before identity is known, these fall back to the
// unscoped legacy names so pre-Step-2 installs keep working. See
// `config.svelte.ts::scopedKey` for details.
const STORAGE_KEY_BASE = 'nymeria-threads';
const CURRENT_THREAD_KEY_BASE = 'nymeria-current-thread';
const FOLDERS_KEY_BASE = 'nymeria-thread-folders';
const TEAM_UI_KEY_BASE = 'nymeria-thread-team-ui';
// `nymeria-thread-organization-mode` (the retired folders/teams view toggle)
// may still sit in old profiles' localStorage; it is simply never read.
const SORT_MODE_KEY_BASE = 'nymeria-thread-sort-mode';
const STORAGE_KEY = () => scopedKey(STORAGE_KEY_BASE);
const CURRENT_THREAD_KEY = () => scopedKey(CURRENT_THREAD_KEY_BASE);
const FOLDERS_KEY = () => scopedKey(FOLDERS_KEY_BASE);
const TEAM_UI_KEY = () => scopedKey(TEAM_UI_KEY_BASE);
const SORT_MODE_KEY = () => scopedKey(SORT_MODE_KEY_BASE);

// Guard against concurrent sync calls (e.g. Vite dev mode double-mount)
let syncInProgress = false;

function loadCurrentThreadId(threads: Thread[]): string | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    const id = localStorage.getItem(CURRENT_THREAD_KEY());
    if (id && threads.some((t) => t.id === id)) return id;
  } catch (e) {
    console.error('Failed to load current thread ID:', e);
  }
  return null;
}

// Moved into createThreadsStore() closure — see saveCurrentThreadId() inside the store

function loadThreads(): Thread[] {
  if (typeof localStorage === 'undefined') return [];

  try {
    const stored = localStorage.getItem(STORAGE_KEY());
    if (stored) {
      const threads = JSON.parse(stored);
      return threads.map((t: Thread) => ({
        ...t,
        createdAt: new Date(t.createdAt),
        updatedAt: new Date(t.updatedAt),
        platform: t.platform || detectThreadPlatform(t.id),
        callable: t.callable ?? t.platform === 'callable',
      }));
    }
  } catch (e) {
    console.error('Failed to load threads:', e);
  }

  return [];
}

function saveThreads(threads: Thread[]): void {
  if (typeof localStorage === 'undefined') return;

  try {
    localStorage.setItem(STORAGE_KEY(), JSON.stringify(threads));
  } catch (e) {
    console.error('Failed to save threads:', e);
  }
}

function loadFolders(): ThreadFolder[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const stored = localStorage.getItem(FOLDERS_KEY());
    if (stored) {
      const folders = JSON.parse(stored);
      return folders.map((f: ThreadFolder) => ({
        ...f,
        createdAt: new Date(f.createdAt),
      }));
    }
  } catch (e) {
    console.error('Failed to load folders:', e);
  }
  return [];
}

function saveFolders(folders: ThreadFolder[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(FOLDERS_KEY(), JSON.stringify(folders));
  } catch (e) {
    console.error('Failed to save folders:', e);
  }
}

function loadTeamUi(): Record<string, { collapsed?: boolean }> {
  if (typeof localStorage === 'undefined') return {};
  try {
    const stored = localStorage.getItem(TEAM_UI_KEY());
    return stored ? JSON.parse(stored) : {};
  } catch (e) {
    console.error('Failed to load team UI state:', e);
  }
  return {};
}

function saveTeamUi(teams: ThreadTeam[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    const state = Object.fromEntries(
      teams.map((team) => [team.id, { collapsed: team.collapsed }])
    );
    localStorage.setItem(TEAM_UI_KEY(), JSON.stringify(state));
  } catch (e) {
    console.error('Failed to save team UI state:', e);
  }
}

function loadSortMode(): SortMode {
  if (typeof localStorage === 'undefined') return 'recent';
  try {
    const stored = localStorage.getItem(SORT_MODE_KEY());
    if (stored && ['recent', 'oldest', 'alphabetical', 'tasks', 'active'].includes(stored)) {
      return stored as SortMode;
    }
  } catch (e) {
    console.error('Failed to load sort mode:', e);
  }
  return 'recent';
}

function saveSortMode(mode: SortMode): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(SORT_MODE_KEY(), mode);
  } catch (e) {
    console.error('Failed to save sort mode:', e);
  }
}

const SPAWNED_FOLDER_NAME = 'Spawned by Nymeria';

function generateTitleFromMessage(message: string, maxLength: number = 40): string {
  // Clean up the message - remove extra whitespace
  const cleaned = message.trim().replace(/\s+/g, ' ');

  if (cleaned.length <= maxLength) {
    return cleaned;
  }

  // Try to truncate at a word boundary
  const truncated = cleaned.slice(0, maxLength);
  const lastSpace = truncated.lastIndexOf(' ');

  if (lastSpace > maxLength * 0.6) {
    return truncated.slice(0, lastSpace) + '…';
  }

  return truncated + '…';
}

function createThreadsStore() {
  const initialThreads = loadThreads();
  let threads = $state<Thread[]>(initialThreads);
  let currentThreadId = $state<string | null>(loadCurrentThreadId(initialThreads));
  let threadTaskCounts = $state<Record<string, number>>({});
  let activeThreadTasks = $state<Set<string>>(new Set());
  let folders = $state<ThreadFolder[]>(loadFolders());
  let threadTeams = $state<ThreadTeam[]>([]);
  let sortMode = $state<SortMode>(loadSortMode());

  // Memoized: the sidebar reads it from several template sites per render
  // and the builder allocates maps and sorts per call.
  const threadSections = $derived.by(() =>
    buildThreadSections({ threads, folders, teams: threadTeams })
  );

  // Gates the UI on the first sync attempt so cached localStorage threads
  // don't render as authoritative before /me + syncFromBackend complete.
  let initialSyncDone = $state(false);
  let lastSyncError = $state<string | null>(null);

  // When the connected user changes (GET /me returns a different id), all
  // four localStorage keys switch to the new user's namespace. Reload from
  // the new scope so we don't keep the previous user's data in memory.
  registerIdentityReloadHook(() => {
    threads = loadThreads();
    currentThreadId = loadCurrentThreadId(threads);
    folders = loadFolders();
    threadTeams = [];
    sortMode = loadSortMode();
    // Re-arm the gate so the new user's first sync controls the sidebar.
    initialSyncDone = false;
    lastSyncError = null;
  });

  function applyThreadTeams(apiTeams: ThreadTeamApi[]): void {
    const ui = loadTeamUi();
    threadTeams = apiTeams
      .map((team) => ({
        id: team.id,
        name: team.name,
        description: team.description ?? null,
        threadIds: team.thread_ids ?? [],
        collapsed: ui[team.id]?.collapsed ?? false,
      }))
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    saveTeamUi(threadTeams);
    unfileTeamedThreads();
  }

  /**
   * A thread in a team is listed under the team, never also under a folder,
   * so folder membership is dropped for every teamed thread whenever the
   * team list is (re)applied. Done here rather than at the two local join
   * sites so joins made elsewhere (an agent spawning into a team, another
   * client, a backend sync) cannot leave a folder holding a ghost member.
   */
  function unfileTeamedThreads(): void {
    const teamed = new Set(threadTeams.flatMap((team) => team.threadIds));
    if (teamed.size === 0) return;
    let changed = false;
    const next = folders.map((f) => {
      const kept = f.threadIds.filter((id) => !teamed.has(id));
      if (kept.length === f.threadIds.length) return f;
      changed = true;
      return { ...f, threadIds: kept };
    });
    if (!changed) return;
    folders = next;
    saveFolders(folders);
  }

  /**
   * Ensure any spawned- threads that aren't in a folder get filed into
   * a "Spawned by Nymeria" folder (creating it lazily). Called after
   * backend syncs or sync-event thread additions.
   */
  function autoFileSpawnedThreads(): void {
    const spawnedIds = threads
      .filter((t) => t.id.startsWith('spawned-'))
      .map((t) => t.id);
    if (spawnedIds.length === 0) return;

    // A teamed spawn is listed under its team; filing it too would leave the
    // folder holding a member the list never shows.
    const filedIds = new Set(folders.flatMap((f) => f.threadIds));
    const teamedIds = new Set(threadTeams.flatMap((team) => team.threadIds));
    const unfiled = spawnedIds.filter((id) => !filedIds.has(id) && !teamedIds.has(id));
    if (unfiled.length === 0) return;

    let spawnedFolder = folders.find((f) => f.name === SPAWNED_FOLDER_NAME);
    if (!spawnedFolder) {
      spawnedFolder = {
        id: generateId(),
        name: SPAWNED_FOLDER_NAME,
        createdAt: new Date(),
        order: folders.length,
        threadIds: [],
        collapsed: false,
      };
      folders = [...folders, spawnedFolder];
    }

    const targetId = spawnedFolder.id;
    folders = folders.map((f) =>
      f.id === targetId
        ? { ...f, threadIds: [...f.threadIds, ...unfiled] }
        : f
    );
    saveFolders(folders);
  }

  function saveCurrentThreadId(id: string | null): void {
    if (typeof localStorage === 'undefined') return;
    try {
      if (id) {
        // Use metadata platform if available, fall back to ID-prefix detection
        const thread = threads.find(t => t.id === id);
        const platform = thread?.platform || detectThreadPlatform(id);
        const platformNativeId = isPlatformNativeThreadId(id);
        // Only skip native platform threads. A desktop-created UUID can still
        // render as Telegram after a chat-app binding and should restore.
        if (platform !== 'desktop' && platform !== 'callable' && platformNativeId) {
          localStorage.removeItem(CURRENT_THREAD_KEY());
          return;
        }
        localStorage.setItem(CURRENT_THREAD_KEY(), id);
      } else {
        localStorage.removeItem(CURRENT_THREAD_KEY());
      }
    } catch (e) {
      console.error('Failed to save current thread ID:', e);
    }
  }

  return {
    get threads() {
      return threads;
    },
    get currentThreadId() {
      return currentThreadId;
    },
    get currentThread(): Thread | undefined {
      return threads.find((t) => t.id === currentThreadId);
    },
    get initialSyncDone() {
      return initialSyncDone;
    },
    get lastSyncError() {
      return lastSyncError;
    },

    // Group threads by date
    get groupedThreads(): ThreadDateGroup[] {
      return bucketThreadsByDate(threads);
    },

    // Folder & sort getters
    get folders() {
      return folders;
    },
    get threadTeams() {
      return threadTeams;
    },
    get sortMode() {
      return sortMode;
    },
    /**
     * The sidebar's grouped view: team and folder sections plus the loose
     * threads, each thread in exactly one place (rules in threadSections.ts).
     */
    get threadSections(): ThreadSections {
      return threadSections;
    },
    /** Threads in no folder and no team: the list's loose tail. */
    get unfiledThreads(): Thread[] {
      return threadSections.loose;
    },
    get sortedUnfiledThreads(): Thread[] {
      const unfiled = this.unfiledThreads;
      // Pin-aware comparator: pinned items always float to top
      const pinFirst = (a: Thread, b: Thread) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
      switch (sortMode) {
        case 'recent':
          return [...unfiled].sort((a, b) => pinFirst(a, b) || b.updatedAt.getTime() - a.updatedAt.getTime());
        case 'oldest':
          return [...unfiled].sort((a, b) => pinFirst(a, b) || a.createdAt.getTime() - b.createdAt.getTime());
        case 'alphabetical':
          return [...unfiled].sort((a, b) => pinFirst(a, b) || a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
        case 'tasks': {
          return [...unfiled].sort((a, b) => {
            const p = pinFirst(a, b);
            if (p !== 0) return p;
            const countDiff = (threadTaskCounts[b.id] ?? 0) - (threadTaskCounts[a.id] ?? 0);
            if (countDiff !== 0) return countDiff;
            return b.updatedAt.getTime() - a.updatedAt.getTime();
          });
        }
        case 'active': {
          return [...unfiled].sort((a, b) => {
            const p = pinFirst(a, b);
            if (p !== 0) return p;
            const aActive = activeThreadTasks.has(a.id) ? 1 : 0;
            const bActive = activeThreadTasks.has(b.id) ? 1 : 0;
            if (bActive !== aActive) return bActive - aActive;
            return b.updatedAt.getTime() - a.updatedAt.getTime();
          });
        }
        default:
          return unfiled;
      }
    },
    get groupedUnfiledThreads(): ThreadDateGroup[] {
      return groupThreadsWithPinned(this.unfiledThreads);
    },

    createThread(title?: string): Thread {
      const thread: Thread = {
        id: generateId(),
        title: title || 'New Thread',
        createdAt: new Date(),
        updatedAt: new Date(),
        messageCount: 0
      };

      threads = [thread, ...threads];
      currentThreadId = thread.id;
      saveCurrentThreadId(currentThreadId);
      saveThreads(threads);

      // NOTE: ownership is claimed lazily, on first real interaction — not
      // here. The backend auto-claims a personal thread on first touch via
      // require_thread_access (POST /chat, command execution, and
      // PATCH /threads/{id}/metadata all run through it). Eagerly claiming
      // at tab-open time used to register every newly-created empty thread in thread_owners
      // immediately, so abandoned tabs became permanent empty backend threads
      // that reappear after a sync (and, with multiple connections, leaked
      // across backends). The TOFU window the old eager claim guarded is not
      // reachable for a freshly generated client UUID: nothing can route a
      // message into it before the user sends the first one.

      return thread;
    },

    selectThread(id: string) {
      if (threads.some((t) => t.id === id)) {
        currentThreadId = id;
        saveCurrentThreadId(currentThreadId);
        // Clear unread flag without bumping updatedAt (preserves sort order)
        const target = threads.find((t) => t.id === id);
        if (target?.unread) {
          threads = threads.map((t) => (t.id === id ? { ...t, unread: false } : t));
          saveThreads(threads);
        }
      }
    },

    markThreadUnread(id: string) {
      const target = threads.find((t) => t.id === id);
      if (!target || target.unread) return;
      threads = threads.map((t) => (t.id === id ? { ...t, unread: true } : t));
      saveThreads(threads);
    },

    updateThread(id: string, updates: Partial<Omit<Thread, 'id' | 'createdAt'>>) {
      threads = threads.map((t) =>
        t.id === id
          ? { ...t, ...updates, updatedAt: new Date() }
          : t
      );
      saveThreads(threads);
    },

    deleteThread(id: string) {
      threads = threads.filter((t) => t.id !== id);
      if (currentThreadId === id) {
        currentThreadId = threads.length > 0 ? threads[0].id : null;
        saveCurrentThreadId(currentThreadId);
      }
      // Remove from any folder
      const folderWithThread = folders.find(f => f.threadIds.includes(id));
      if (folderWithThread) {
        folders = folders.map(f =>
          f.id === folderWithThread.id
            ? { ...f, threadIds: f.threadIds.filter(tid => tid !== id) }
            : f
        );
        saveFolders(folders);
      }
      threadTeams = threadTeams.map(team => ({
        ...team,
        threadIds: team.threadIds.filter(tid => tid !== id),
      })).filter(team => team.threadIds.length > 0);
      saveTeamUi(threadTeams);
      saveThreads(threads);

      // Write-through: delete metadata, checkpoints, and config on backend
      api.deleteThread(id).catch((err) => {
        console.warn('[Threads] Failed to delete thread on backend:', err);
      });
    },

    setThreadFromApi(id: string, title: string) {
      const existing = threads.find((t) => t.id === id);
      if (!existing) {
        const thread: Thread = {
          id,
          title,
          createdAt: new Date(),
          updatedAt: new Date(),
          messageCount: 0,
          platform: detectThreadPlatform(id),
        };
        threads = [thread, ...threads];
        saveThreads(threads);
      }
      currentThreadId = id;
      saveCurrentThreadId(currentThreadId);
    },

    /**
     * Ensure a thread exists in the sidebar without switching to it.
     * Creates the thread if missing; updates its title if different.
     */
    ensureThread(id: string, title: string) {
      const existing = threads.find((t) => t.id === id);
      if (!existing) {
        const thread: Thread = {
          id,
          title,
          createdAt: new Date(),
          updatedAt: new Date(),
          messageCount: 0,
          platform: detectThreadPlatform(id),
        };
        threads = [thread, ...threads];
        saveThreads(threads);
      } else if (existing.title !== title) {
        threads = threads.map((t) =>
          t.id === id ? { ...t, title, updatedAt: new Date() } : t
        );
        saveThreads(threads);
      }
    },

    /**
     * Auto-generate a title from the first user message if the thread
     * still has a default title ("New Thread" for new threads, or
     * "New Chat" for threads created before the §9 terminology canon
     * sweep).
     */
    autoTitleFromMessage(id: string, message: string) {
      const thread = threads.find((t) => t.id === id);
      if (thread && (thread.title === 'New Thread' || thread.title === 'New Chat')) {
        const newTitle = generateTitleFromMessage(message);
        threads = threads.map((t) =>
          t.id === id ? { ...t, title: newTitle, updatedAt: new Date() } : t
        );
        saveThreads(threads);
      }
    },

    /**
     * Update a thread's title from backend data (e.g. auto-title in SSE done event).
     * Local-only — no write-through since the title originates from the server.
     */
    applyBackendTitle(id: string, title: string) {
      if (!title) return;
      threads = threads.map((t) =>
        t.id === id ? { ...t, title, updatedAt: new Date() } : t
      );
      saveThreads(threads);
    },

    /**
     * Manually rename a thread (user-initiated).
     * Optimistically updates localStorage, then writes through to backend.
     */
    renameThread(id: string, newTitle: string) {
      const trimmed = newTitle.trim();
      if (!trimmed) return;

      threads = threads.map((t) =>
        t.id === id ? { ...t, title: trimmed, updatedAt: new Date() } : t
      );
      saveThreads(threads);

      // Write-through to backend
      api.updateThreadMetadata(id, { title: trimmed }).catch((err) => {
        console.warn('[Threads] Failed to sync rename to backend:', err);
      });
    },

    clearCurrent() {
      currentThreadId = null;
      saveCurrentThreadId(null);
    },

    /**
     * Full reset for connection switching. Wipes all local thread data
     * so syncFromBackend() starts fresh against a different backend.
     */
    reset() {
      threads = [];
      folders = [];
      threadTeams = [];
      currentThreadId = null;
      syncInProgress = false;
      initialSyncDone = false;
      lastSyncError = null;
      saveThreads([]);
      saveFolders([]);
      saveTeamUi([]);
      saveCurrentThreadId(null);
    },

    /**
     * Sync the local thread list with the backend (server-side metadata).
     *
     * Backend is authoritative for titles and pins. On first sync (migration),
     * local data is pushed to the backend so existing titles/pins are preserved.
     * Subsequent syncs merge backend data into localStorage.
     */
    async syncFromBackend() {
      if (syncInProgress) return;
      syncInProgress = true;
      lastSyncError = null;
      try {
        const response = await api.listThreadsWithMetadata();
        // Teams first: applying threads auto-files spawned ones into a
        // folder, which must skip threads the backend already teamed.
        await this.loadThreadTeams();
        // Backend is authoritative for the thread list, titles, and pins.
        this._applyBackendThreads(response.threads);
      } catch (e) {
        lastSyncError = e instanceof Error ? e.message : String(e);
        console.warn('[Threads] Backend sync failed:', e);
      } finally {
        syncInProgress = false;
        initialSyncDone = true;
      }
    },

    /**
     * Apply backend thread data to the local store.
     * Backend is authoritative — local data is replaced.
     */
    _applyBackendThreads(backendThreads: Array<{
      thread_id: string;
      title: string;
      pinned: boolean;
      platform: string;
      platform_meta: Record<string, string> | null;
      callable?: boolean;
      created_at: string | null;
      updated_at: string | null;
      title_source: string;
      recovered?: boolean;
      recovery_sources?: string[];
    }>) {
      // Build a map of local threads for preserving UI-only state
      const localMap = new Map(threads.map((t) => [t.id, t]));

      const merged: Thread[] = backendThreads.map((bt) => {
        const local = localMap.get(bt.thread_id);
        return {
          id: bt.thread_id,
          title: bt.title || local?.title || 'New Thread',
          pinned: bt.pinned ?? local?.pinned ?? false,
          platform: (bt.platform as ThreadPlatform) || detectThreadPlatform(bt.thread_id),
          callable: bt.callable ?? local?.callable ?? bt.platform === 'callable',
          platformMeta: bt.platform_meta ? {
            guildName: bt.platform_meta.guild_name,
            channelName: bt.platform_meta.channel_name,
            guildId: bt.platform_meta.guild_id,
            channelId: bt.platform_meta.channel_id,
          } : local?.platformMeta,
          createdAt: bt.created_at ? new Date(bt.created_at) : local?.createdAt ?? new Date(),
          updatedAt: bt.updated_at ? new Date(bt.updated_at) : local?.updatedAt ?? new Date(),
          messageCount: local?.messageCount ?? 0,
          hasCustomConfig: local?.hasCustomConfig,
          recovered: bt.recovered ?? local?.recovered ?? false,
          recoverySources: bt.recovery_sources ?? local?.recoverySources ?? [],
        };
      });

      threads = merged;
      if (currentThreadId && !threads.some((t) => t.id === currentThreadId)) {
        currentThreadId = null;
        saveCurrentThreadId(null);
      }
      saveThreads(threads);
      autoFileSpawnedThreads();
      debugLog('[Threads] Synced from backend:', backendThreads.length, 'threads');
    },

    // Thread task badge support
    setThreadTaskCounts(counts: Record<string, number>) {
      threadTaskCounts = counts;
    },

    getThreadTaskCount(threadId: string): number {
      return threadTaskCounts[threadId] ?? 0;
    },

    setThreadActive(threadId: string, active: boolean) {
      const next = new Set(activeThreadTasks);
      if (active) {
        next.add(threadId);
      } else {
        next.delete(threadId);
      }
      activeThreadTasks = next;
    },

    isThreadActive(threadId: string): boolean {
      return activeThreadTasks.has(threadId);
    },

    // Sort methods
    setSortMode(mode: SortMode) {
      sortMode = mode;
      saveSortMode(mode);
    },

    async loadThreadTeams() {
      try {
        const teams = await api.listThreadTeams();
        applyThreadTeams(teams);
      } catch (e) {
        console.warn('[Threads] Failed to load thread teams:', e);
      }
    },

    // Joining a team lifts a thread out of its folder (applyThreadTeams).
    async createThreadTeam(name: string, threadIds: string[]): Promise<ThreadTeam | null> {
      const team = await api.createThreadTeam({ name, thread_ids: threadIds });
      applyThreadTeams(await api.listThreadTeams());
      return threadTeams.find((t) => t.id === team.id) ?? null;
    },

    async renameThreadTeam(id: string, name: string) {
      await api.updateThreadTeam(id, { name });
      applyThreadTeams(await api.listThreadTeams());
    },

    async describeThreadTeam(id: string, description: string) {
      // Empty string clears the description (REST semantics).
      await api.updateThreadTeam(id, { description });
      applyThreadTeams(await api.listThreadTeams());
    },

    async addThreadsToTeam(teamId: string, threadIds: string[]) {
      const current = threadTeams.find((team) => team.id === teamId);
      if (!current) return;
      const merged = [...new Set([...current.threadIds, ...threadIds])];
      await api.updateThreadTeam(teamId, { thread_ids: merged });
      applyThreadTeams(await api.listThreadTeams());
    },

    async deleteThreadTeam(id: string) {
      await api.deleteThreadTeam(id);
      applyThreadTeams(await api.listThreadTeams());
    },

    toggleTeamCollapse(id: string) {
      threadTeams = threadTeams.map(team =>
        team.id === id ? { ...team, collapsed: !team.collapsed } : team
      );
      saveTeamUi(threadTeams);
    },

    // Folder methods
    createFolder(name: string): ThreadFolder {
      const folder: ThreadFolder = {
        id: generateId(),
        name,
        createdAt: new Date(),
        order: folders.length,
        threadIds: [],
        collapsed: false,
      };
      folders = [...folders, folder];
      saveFolders(folders);
      return folder;
    },

    renameFolder(id: string, name: string) {
      folders = folders.map(f =>
        f.id === id ? { ...f, name } : f
      );
      saveFolders(folders);
    },

    deleteFolder(id: string) {
      folders = folders.filter(f => f.id !== id);
      saveFolders(folders);
    },

    toggleFolderCollapse(id: string) {
      folders = folders.map(f =>
        f.id === id ? { ...f, collapsed: !f.collapsed } : f
      );
      saveFolders(folders);
    },

    addThreadsToFolder(folderId: string, threadIds: string[]) {
      const idsSet = new Set(threadIds);
      // Remove from any existing folders first (single-folder constraint)
      folders = folders.map(f => ({
        ...f,
        threadIds: f.id === folderId
          ? [...f.threadIds.filter(tid => !idsSet.has(tid)), ...threadIds]
          : f.threadIds.filter(tid => !idsSet.has(tid)),
      }));
      saveFolders(folders);
    },

    removeThreadsFromFolder(threadIds: string[]) {
      const idsSet = new Set(threadIds);
      folders = folders.map(f => ({
        ...f,
        threadIds: f.threadIds.filter(tid => !idsSet.has(tid)),
      }));
      saveFolders(folders);
    },

    // Pin methods
    togglePinThread(id: string) {
      const thread = threads.find(t => t.id === id);
      const newPinned = !(thread?.pinned);
      threads = threads.map(t =>
        t.id === id ? { ...t, pinned: newPinned } : t
      );
      saveThreads(threads);

      // Write-through to backend
      api.updateThreadMetadata(id, { pinned: newPinned }).catch((err) => {
        console.warn('[Threads] Failed to sync pin to backend:', err);
      });
    },

    togglePinFolder(id: string) {
      folders = folders.map(f =>
        f.id === id ? { ...f, pinned: !f.pinned } : f
      );
      saveFolders(folders);
    },

    isThreadPinned(id: string): boolean {
      return threads.find(t => t.id === id)?.pinned ?? false;
    },

    isFolderPinned(id: string): boolean {
      return folders.find(f => f.id === id)?.pinned ?? false;
    },

    // ====================================================================
    // Cross-client sync methods (local-only, no backend write-through)
    // Used when another client changed state and the event arrived via SSE.
    // ====================================================================

    /**
     * Update a thread's metadata from a sync event (no backend write-through).
     * Used when another client renamed/pinned a thread.
     */
    updateThreadFromSync(id: string, updates: Partial<Pick<Thread, 'title' | 'pinned' | 'platform'>>) {
      const existing = threads.find(t => t.id === id);
      if (!existing) return;
      threads = threads.map(t =>
        t.id === id ? { ...t, ...updates, updatedAt: new Date() } : t
      );
      saveThreads(threads);
    },

    /**
     * Remove a thread from the local store (no backend write-through).
     * Used when another client deleted a thread.
     */
    deleteThreadLocal(id: string) {
      threads = threads.filter(t => t.id !== id);
      if (currentThreadId === id) {
        currentThreadId = threads.length > 0 ? threads[0].id : null;
        saveCurrentThreadId(currentThreadId);
      }
      // Remove from any folder
      const folderWithThread = folders.find(f => f.threadIds.includes(id));
      if (folderWithThread) {
        folders = folders.map(f =>
          f.id === folderWithThread.id
            ? { ...f, threadIds: f.threadIds.filter(tid => tid !== id) }
            : f
        );
        saveFolders(folders);
      }
      threadTeams = threadTeams.map(team => ({
        ...team,
        threadIds: team.threadIds.filter(tid => tid !== id),
      })).filter(team => team.threadIds.length > 0);
      saveTeamUi(threadTeams);
      saveThreads(threads);
    },

    /**
     * Add a new thread from a sync event (no backend write-through).
     * Used when another client created a callable thread or new thread.
     */
    addThreadFromSync(id: string, title: string, platform?: ThreadPlatform) {
      if (threads.some(t => t.id === id)) return; // already exists
      const thread: Thread = {
        id,
        title,
        createdAt: new Date(),
        updatedAt: new Date(),
        messageCount: 0,
        platform: platform || detectThreadPlatform(id),
      };
      threads = [thread, ...threads];
      saveThreads(threads);
      if (id.startsWith('spawned-')) {
        autoFileSpawnedThreads();
      }
    },

    /**
     * Bump a thread's updatedAt timestamp (e.g. when activity happens on another thread).
     */
    touchThread(id: string) {
      const existing = threads.find(t => t.id === id);
      if (!existing) return;
      threads = threads.map(t =>
        t.id === id ? { ...t, updatedAt: new Date() } : t
      );
      saveThreads(threads);
    },
  };
}

export const threadsStore = createThreadsStore();
