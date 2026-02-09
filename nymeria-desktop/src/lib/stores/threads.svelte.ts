import type { Thread } from '$lib/types';

const STORAGE_KEY = 'nymeria-threads';

function loadThreads(): Thread[] {
  if (typeof localStorage === 'undefined') return [];

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const threads = JSON.parse(stored);
      return threads.map((t: Thread) => ({
        ...t,
        createdAt: new Date(t.createdAt),
        updatedAt: new Date(t.updatedAt)
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
  let currentThreadId = $state<string | null>(null);
  let threadTaskCounts = $state<Record<string, number>>({});
  let activeThreadTasks = $state<Set<string>>(new Set());

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
      saveThreads(threads);

      return thread;
    },

    selectThread(id: string) {
      if (threads.some((t) => t.id === id)) {
        currentThreadId = id;
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
          messageCount: 0
        };
        threads = [thread, ...threads];
        saveThreads(threads);
      }
      currentThreadId = id;
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
    }
  };
}

export const threadsStore = createThreadsStore();
