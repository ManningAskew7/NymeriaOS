import { describe, it, expect } from 'vitest';
import type { Thread } from '$lib/types';
import { bucketThreadsByDate, groupThreadsWithPinned } from './threadGrouping';

function makeThread(id: string, updatedAt: Date, pinned = false): Thread {
  return {
    id,
    title: id,
    createdAt: updatedAt,
    updatedAt,
    messageCount: 0,
    pinned,
  };
}

// Fixed reference time so the date boundaries are deterministic.
// today = 2026-06-25, yesterday = 2026-06-24, weekAgo = 2026-06-18.
const NOW = new Date(2026, 5, 25, 15, 0, 0);

describe('bucketThreadsByDate', () => {
  it('buckets threads into the four date groups by updatedAt', () => {
    const threads = [
      makeThread('today-am', new Date(2026, 5, 25, 9, 0, 0)),
      makeThread('today-pm', new Date(2026, 5, 25, 14, 0, 0)),
      makeThread('yesterday', new Date(2026, 5, 24, 20, 0, 0)),
      makeThread('this-week', new Date(2026, 5, 20, 10, 0, 0)),
      makeThread('older', new Date(2026, 5, 1, 10, 0, 0)),
    ];

    const groups = bucketThreadsByDate(threads, NOW);

    expect(groups.map((g) => g.label)).toEqual([
      'Today',
      'Yesterday',
      'Previous 7 Days',
      'Older',
    ]);
    // Within Today, newest first.
    expect(groups[0].threads.map((t) => t.id)).toEqual(['today-pm', 'today-am']);
    expect(groups[1].threads.map((t) => t.id)).toEqual(['yesterday']);
    expect(groups[2].threads.map((t) => t.id)).toEqual(['this-week']);
    expect(groups[3].threads.map((t) => t.id)).toEqual(['older']);
  });

  it('drops empty buckets', () => {
    const groups = bucketThreadsByDate(
      [makeThread('a', new Date(2026, 5, 25, 8, 0, 0))],
      NOW
    );
    expect(groups.map((g) => g.label)).toEqual(['Today']);
  });

  it('returns an empty array for no threads', () => {
    expect(bucketThreadsByDate([], NOW)).toEqual([]);
  });

  it('treats exactly seven days ago as Previous 7 Days and eight days ago as Older', () => {
    const sevenDaysAgo = makeThread('seven', new Date(2026, 5, 18, 23, 0, 0));
    const eightDaysAgo = makeThread('eight', new Date(2026, 5, 17, 1, 0, 0));
    const groups = bucketThreadsByDate([sevenDaysAgo, eightDaysAgo], NOW);

    const byLabel = Object.fromEntries(groups.map((g) => [g.label, g.threads.map((t) => t.id)]));
    expect(byLabel['Previous 7 Days']).toEqual(['seven']);
    expect(byLabel['Older']).toEqual(['eight']);
    expect(byLabel['Today']).toBeUndefined();
  });

  it('sorts newest-first across the whole input regardless of input order', () => {
    const threads = [
      makeThread('old', new Date(2026, 5, 1, 10, 0, 0)),
      makeThread('newest', new Date(2026, 5, 25, 14, 0, 0)),
      makeThread('mid', new Date(2026, 5, 25, 9, 0, 0)),
    ];
    const groups = bucketThreadsByDate(threads, NOW);
    expect(groups[0].threads.map((t) => t.id)).toEqual(['newest', 'mid']);
  });

  it('does not mutate the input array order', () => {
    const threads = [
      makeThread('a', new Date(2026, 5, 1, 10, 0, 0)),
      makeThread('b', new Date(2026, 5, 25, 14, 0, 0)),
    ];
    bucketThreadsByDate(threads, NOW);
    expect(threads.map((t) => t.id)).toEqual(['a', 'b']);
  });

  it('keeps equal-timestamp threads in their input order (stable sort)', () => {
    const ts = new Date(2026, 5, 25, 9, 0, 0);
    const groups = bucketThreadsByDate([makeThread('first', ts), makeThread('second', ts)], NOW);
    expect(groups[0].threads.map((t) => t.id)).toEqual(['first', 'second']);
  });

  it('uses the current time when now is omitted (smoke)', () => {
    const groups = bucketThreadsByDate([makeThread('x', new Date())]);
    expect(groups[0].label).toBe('Today');
  });
});

describe('groupThreadsWithPinned', () => {
  it('pulls pinned threads into a leading newest-first group regardless of their date', () => {
    const threads = [
      makeThread('pinned-old', new Date(2026, 5, 1, 10, 0, 0), true),
      makeThread('pinned-recent', new Date(2026, 5, 25, 14, 0, 0), true),
      makeThread('today', new Date(2026, 5, 25, 9, 0, 0)),
      makeThread('this-week', new Date(2026, 5, 20, 10, 0, 0)),
    ];

    const groups = groupThreadsWithPinned(threads, NOW);

    expect(groups.map((g) => g.label)).toEqual(['Pinned', 'Today', 'Previous 7 Days']);
    // Pinned group is newest-first and holds both pinned threads despite their
    // different dates.
    expect(groups[0].threads.map((t) => t.id)).toEqual(['pinned-recent', 'pinned-old']);
    expect(groups[1].threads.map((t) => t.id)).toEqual(['today']);
    expect(groups[2].threads.map((t) => t.id)).toEqual(['this-week']);
  });

  it('omits the Pinned group entirely when nothing is pinned', () => {
    const threads = [
      makeThread('today', new Date(2026, 5, 25, 9, 0, 0)),
      makeThread('older', new Date(2026, 5, 1, 10, 0, 0)),
    ];
    const groups = groupThreadsWithPinned(threads, NOW);
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Older']);
  });

  it('returns only the Pinned group when every thread is pinned', () => {
    const threads = [
      makeThread('a', new Date(2026, 5, 25, 9, 0, 0), true),
      makeThread('b', new Date(2026, 5, 1, 10, 0, 0), true),
    ];
    const groups = groupThreadsWithPinned(threads, NOW);
    expect(groups.map((g) => g.label)).toEqual(['Pinned']);
    expect(groups[0].threads.map((t) => t.id)).toEqual(['a', 'b']);
  });
});
