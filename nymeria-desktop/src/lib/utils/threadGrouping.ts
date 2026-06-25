import type { Thread } from '$lib/types';

export interface ThreadDateGroup {
  label: string;
  threads: Thread[];
}

// Bucket threads into Today / Yesterday / Previous 7 Days / Older by their
// updatedAt calendar date, newest first within each bucket, dropping empty
// buckets. `now` is injectable so the date boundaries can be pinned in tests;
// it defaults to the current time. Pure: no store or DOM dependency. This is the
// date math the grouped-thread store getters previously open-coded in two places.
export function bucketThreadsByDate(
  threads: Thread[],
  now: Date = new Date()
): ThreadDateGroup[] {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);

  const groups: ThreadDateGroup[] = [
    { label: 'Today', threads: [] },
    { label: 'Yesterday', threads: [] },
    { label: 'Previous 7 Days', threads: [] },
    { label: 'Older', threads: [] },
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
}

// Like bucketThreadsByDate, but pinned threads are pulled into a leading
// "Pinned" group (kept in newest-first order) instead of being date-bucketed.
// Used for the unfiled-threads view. Sorting happens once up front so the pinned
// slice inherits the newest-first order before it is split out.
export function groupThreadsWithPinned(
  threads: Thread[],
  now: Date = new Date()
): ThreadDateGroup[] {
  const sorted = [...threads].sort(
    (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
  );
  const pinned = sorted.filter((t) => t.pinned);
  const dated = bucketThreadsByDate(sorted.filter((t) => !t.pinned), now);
  return pinned.length > 0
    ? [{ label: 'Pinned', threads: pinned }, ...dated]
    : dated;
}
