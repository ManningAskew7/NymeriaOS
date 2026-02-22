import type { Thread, ThreadPlatform, ThreadFolder, SortMode } from '$lib/types';

const STORAGE_KEY = 'nymeria-threads';
const CURRENT_THREAD_KEY = 'nymeria-current-thread';
const FOLDERS_KEY = 'nymeria-thread-folders';
const SORT_MODE_KEY = 'nymeria-thread-sort-mode';

function loadCurrentThreadId(threads: Thread[]): string | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    const id = localStorage.getItem(CURRENT_THREAD_KEY);
    if (id && threads.some((t) => t.id === id)) return id;
  } catch (e) {
    console.error('Failed to load current thread ID:', e);
  }
  return null;
}

function saveCurrentThreadId(id: string | null): void {
  if (typeof localStorage === 'undefined') return;
  try {
    if (id) {
      // Don't persist non-desktop thread IDs (trigger, discord, telegram, slack)
      // so they won't be restored on app restart. The in-memory currentThreadId
      // still updates normally for within-session navigation.
      if (detectPlatform(id) !== 'desktop') {
        localStorage.removeItem(CURRENT_THREAD_KEY);
        return;
      }
      localStorage.setItem(CURRENT_THREAD_KEY, id);
    } else {
      localStorage.removeItem(CURRENT_THREAD_KEY);
    }
  } catch (e) {
    console.error('Failed to save current thread ID:', e);
  }
}

function loadThreads(): Thread[] {
  if (typeof localStorage === 'undefined') return [];

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const threads = JSON.parse(stored);
      return threads.map((t: Thread) => ({
        ...t,
        createdAt: new Date(t.createdAt),
        updatedAt: new Date(t.updatedAt),
        platform: t.platform || detectPlatform(t.id),
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
    localStorage.setItem(STORAGE_KEY, JSON.stringify(threads));
  } catch (e) {
    console.error('Failed to save threads:', e);
  }
}

function loadFolders(): ThreadFolder[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const stored = localStorage.getItem(FOLDERS_KEY);
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
    localStorage.setItem(FOLDERS_KEY, JSON.stringify(folders));
  } catch (e) {
    console.error('Failed to save folders:', e);
  }
}

function loadSortMode(): SortMode {
  if (typeof localStorage === 'undefined') return 'recent';
  try {
    const stored = localStorage.getItem(SORT_MODE_KEY);
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
    localStorage.setItem(SORT_MODE_KEY, mode);
  } catch (e) {
    console.error('Failed to save sort mode:', e);
  }
}

function detectPlatform(threadId: string): ThreadPlatform {
  if (threadId.startsWith('discord_')) return 'discord';
  if (threadId.startsWith('telegram_')) return 'telegram';
  if (threadId.startsWith('slack_')) return 'slack';
  if (threadId.startsWith('trigger-')) return 'trigger';
  return 'desktop';
}

function generateId(): string {
  return crypto.randomUUID();
}

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
    return truncated.slice(0, lastSpace) + '...';
  }

  return truncated + '...';
}

function createThreadsStore() {
  let threads = $state<Thread[]>(loadThreads());
  let currentThreadId = $state<string | null>(loadCurrentThreadId(threads));
  let threadTaskCounts = $state<Record<string, number>>({});
  let activeThreadTasks = $state<Set<string>>(new Set());
  let folders = $state<ThreadFolder[]>(loadFolders());
  let sortMode = $state<SortMode>(loadSortMode());

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

    // Group threads by date
    get groupedThreads(): { label: string; threads: Thread[] }[] {
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      const weekAgo = new Date(today);
      weekAgo.setDate(weekAgo.getDate() - 7);

      const groups: { label: string; threads: Thread[] }[] = [
        { label: 'Today', threads: [] },
        { label: 'Yesterday', threads: [] },
        { label: 'Previous 7 Days', threads: [] },
        { label: 'Older', threads: [] }
      ];

      const sorted = [...threads].sort(
        (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
      );

      for (const thread of sorted) {
        const threadDate = new Date(
          thread.updatedAt.getFullYear(),
          thread.updatedAt.getMonth(),
          thread.updatedAt.getDate()
        );

        if (threadDate >= today) {
          groups[0].threads.push(thread);
        } else if (threadDate >= yesterday) {
          groups[1].threads.push(thread);
        } else if (threadDate >= weekAgo) {
          groups[2].threads.push(thread);
        } else {
          groups[3].threads.push(thread);
        }
      }

      return groups.filter((g) => g.threads.length > 0);
    },

    // Folder & sort getters
    get folders() {
      return folders;
    },
    get sortMode() {
      return sortMode;
    },
    get unfiledThreads(): Thread[] {
      const filedIds = new Set(folders.flatMap(f => f.threadIds));
      return threads.filter(t => !filedIds.has(t.id));
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
    get groupedUnfiledThreads(): { label: string; threads: Thread[] }[] {
      const unfiled = this.unfiledThreads;
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
        { label: 'Older', threads: [] }
      ];

      const sorted = [...unfiled].sort(
        (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
      );

      for (const thread of sorted) {
        if (thread.pinned) {
          pinnedGroup.threads.push(thread);
          continue;
        }

        const threadDate = new Date(
          thread.updatedAt.getFullYear(),
          thread.updatedAt.getMonth(),
          thread.updatedAt.getDate()
        );

        if (threadDate >= today) {
          groups[0].threads.push(thread);
        } else if (threadDate >= yesterday) {
          groups[1].threads.push(thread);
        } else if (threadDate >= weekAgo) {
          groups[2].threads.push(thread);
        } else {
          groups[3].threads.push(thread);
        }
      }

      const result: { label: string; threads: Thread[] }[] = [];
      if (pinnedGroup.threads.length > 0) result.push(pinnedGroup);
      for (const g of groups) {
        if (g.threads.length > 0) result.push(g);
      }
      return result;
    },

    createThread(title?: string): Thread {
      const thread: Thread = {
        id: generateId(),
        title: title || 'New Chat',
        createdAt: new Date(),
        updatedAt: new Date(),
        messageCount: 0
      };

      threads = [thread, ...threads];
      currentThreadId = thread.id;
      saveCurrentThreadId(currentThreadId);
      saveThreads(threads);

      return thread;
    },

    selectThread(id: string) {
      if (threads.some((t) => t.id === id)) {
        currentThreadId = id;
        saveCurrentThreadId(currentThreadId);
      }
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
      saveThreads(threads);
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
          platform: detectPlatform(id),
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
          platform: detectPlatform(id),
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
     * still has the default "New Chat" title.
     */
    autoTitleFromMessage(id: string, message: string) {
      const thread = threads.find((t) => t.id === id);
      if (thread && thread.title === 'New Chat') {
        const newTitle = generateTitleFromMessage(message);
        threads = threads.map((t) =>
          t.id === id ? { ...t, title: newTitle, updatedAt: new Date() } : t
        );
        saveThreads(threads);
      }
    },

    /**
     * Manually rename a thread (user-initiated)
     */
    renameThread(id: string, newTitle: string) {
      const trimmed = newTitle.trim();
      if (!trimmed) return;

      threads = threads.map((t) =>
        t.id === id ? { ...t, title: trimmed, updatedAt: new Date() } : t
      );
      saveThreads(threads);
    },

    clearCurrent() {
      currentThreadId = null;
      saveCurrentThreadId(null);
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
      threads = threads.map(t =>
        t.id === id ? { ...t, pinned: !t.pinned } : t
      );
      saveThreads(threads);
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
  };
}

export const threadsStore = createThreadsStore();
