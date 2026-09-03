import { describe, expect, it } from 'vitest';
import type { Thread, ThreadFolder, ThreadTeam } from '$lib/types';
import { buildThreadSections, teamedThreadIds, visibleThreadIds } from './threadSections';

function thread(id: string, pinned = false): Thread {
  return {
    id,
    title: id,
    createdAt: new Date('2026-09-01T00:00:00Z'),
    updatedAt: new Date('2026-09-02T00:00:00Z'),
    messageCount: 0,
    pinned,
  };
}

function folder(id: string, threadIds: string[], extra: Partial<ThreadFolder> = {}): ThreadFolder {
  return { id, name: id, createdAt: new Date(0), order: 0, threadIds, collapsed: false, ...extra };
}

function team(id: string, threadIds: string[]): ThreadTeam {
  return { id, name: id, description: null, threadIds, collapsed: false };
}

const threads = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => thread(id));

describe('buildThreadSections', () => {
  it('lists every team and every folder as a section, loose threads apart', () => {
    const { sections, loose } = buildThreadSections({
      threads,
      folders: [folder('F', ['a'])],
      teams: [team('T', ['b'])],
    });
    expect(sections.map((s) => `${s.kind}:${s.group.id}`)).toEqual(['team:T', 'folder:F']);
    expect(loose.map((t) => t.id)).toEqual(['c', 'd', 'e', 'f']);
  });

  it('shows a thread in both a team and a folder under the team only', () => {
    const { sections, loose } = buildThreadSections({
      threads,
      folders: [folder('F', ['a', 'b'])],
      teams: [team('T', ['a'])],
    });
    const byId = Object.fromEntries(sections.map((s) => [s.group.id, s.threads.map((t) => t.id)]));
    expect(byId.T).toEqual(['a']);
    expect(byId.F).toEqual(['b']);
    expect(loose.map((t) => t.id)).not.toContain('a');
    // Every thread appears exactly once across sections + loose.
    const all = [...sections.flatMap((s) => s.threads), ...loose].map((t) => t.id).sort();
    expect(all).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
  });

  it('orders pinned folders, then teams, then remaining folders by order', () => {
    const { sections } = buildThreadSections({
      threads,
      folders: [
        folder('F2', ['a'], { order: 2 }),
        folder('P', ['b'], { order: 5, pinned: true }),
        folder('F1', ['c'], { order: 1 }),
      ],
      teams: [team('T', ['d'])],
    });
    expect(sections.map((s) => s.group.id)).toEqual(['P', 'T', 'F1', 'F2']);
  });

  it('keeps group order inside a section with pinned threads first', () => {
    const pinnedC = thread('c', true);
    const { sections } = buildThreadSections({
      threads: [thread('a'), thread('b'), pinnedC],
      folders: [],
      teams: [team('T', ['a', 'b', 'c'])],
    });
    expect(sections[0].threads.map((t) => t.id)).toEqual(['c', 'a', 'b']);
  });

  it('drops member ids that no longer resolve to a thread', () => {
    const { sections } = buildThreadSections({
      threads: [thread('a')],
      folders: [folder('F', ['a', 'gone'])],
      teams: [team('T', ['missing'])],
    });
    expect(sections.find((s) => s.group.id === 'T')?.threads).toEqual([]);
    expect(sections.find((s) => s.group.id === 'F')?.threads.map((t) => t.id)).toEqual(['a']);
  });

  it('adapts a team into the folder shape FolderItem renders, keeping its collapsed state', () => {
    const collapsed = { ...team('T', ['a']), collapsed: true };
    const { sections } = buildThreadSections({ threads, folders: [], teams: [collapsed] });
    expect(sections[0].kind).toBe('team');
    expect(sections[0].group).toMatchObject({ id: 'T', name: 'T', collapsed: true, threadIds: ['a'] });
  });
});

describe('visibleThreadIds', () => {
  it('walks expanded sections in order, skips collapsed ones, then the loose tail as given', () => {
    const { sections, loose } = buildThreadSections({
      threads,
      folders: [folder('F', ['a', 'b'], { collapsed: true }), folder('G', ['c'])],
      teams: [team('T', ['d'])],
    });
    const looseInOrder = [...loose].reverse();
    expect(visibleThreadIds(sections, looseInOrder)).toEqual(['d', 'c', 'f', 'e']);
  });
});

describe('teamedThreadIds', () => {
  it('returns only the ids that belong to a team, in selection order', () => {
    const teams = [team('T1', ['b']), team('T2', ['d', 'e'])];
    expect(teamedThreadIds(teams, ['a', 'e', 'b', 'c'])).toEqual(['e', 'b']);
    expect(teamedThreadIds([], ['a'])).toEqual([]);
  });
});
