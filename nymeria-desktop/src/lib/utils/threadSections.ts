/**
 * The sidebar's single thread list: teams and folders as sections, then the
 * loose threads.
 *
 * Folders are client-side and per device; teams are the backend's callable
 * teams, which change agent behavior and sync everywhere. Both are flat and
 * exclusive within their own kind, but nothing stops a thread being in one of
 * each (an agent can spawn a thread straight into a team). The list still
 * shows each thread ONCE: team membership wins, since that is the grouping
 * the agent acts on; the folder is personal tidy-up around it.
 *
 * Section order: pinned folders, teams (in the order the store keeps them,
 * alphabetical), remaining folders by `order`. Pure so the rules are
 * unit-testable without a DOM.
 */
import type { Thread, ThreadFolder, ThreadTeam } from '$lib/types';

export interface ThreadSection {
  kind: 'team' | 'folder';
  /** The group in FolderItem's shape (a team is adapted into it). */
  group: ThreadFolder;
  /** Member threads in group order, pinned first. */
  threads: Thread[];
}

export interface ThreadSections {
  sections: ThreadSection[];
  /** Threads in no team and no folder. */
  loose: Thread[];
}

function pinFirst(a: Thread, b: Thread): number {
  return (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
}

function resolve(ids: string[], byId: Map<string, Thread>): Thread[] {
  return ids
    .map((id) => byId.get(id))
    .filter((t): t is Thread => t !== undefined)
    .sort(pinFirst);
}

/** Ids among `ids` that belong to some team (the Folder action refuses these). */
export function teamedThreadIds(teams: ThreadTeam[], ids: Iterable<string>): string[] {
  const teamed = new Set(teams.flatMap((team) => team.threadIds));
  return [...ids].filter((id) => teamed.has(id));
}

export function buildThreadSections(input: {
  threads: Thread[];
  folders: ThreadFolder[];
  teams: ThreadTeam[];
}): ThreadSections {
  const byId = new Map(input.threads.map((t) => [t.id, t]));
  const teamed = new Set(input.teams.flatMap((team) => team.threadIds));

  const folderSection = (folder: ThreadFolder): ThreadSection => ({
    kind: 'folder',
    group: folder,
    threads: resolve(
      folder.threadIds.filter((id) => !teamed.has(id)),
      byId
    ),
  });
  const teamSection = (team: ThreadTeam): ThreadSection => ({
    kind: 'team',
    group: {
      id: team.id,
      name: team.name,
      createdAt: new Date(0),
      order: 0,
      threadIds: team.threadIds,
      collapsed: team.collapsed,
    },
    threads: resolve(team.threadIds, byId),
  });

  const byOrder = (a: ThreadFolder, b: ThreadFolder) => a.order - b.order;
  const pinnedFolders = input.folders.filter((f) => f.pinned).sort(byOrder);
  const otherFolders = input.folders.filter((f) => !f.pinned).sort(byOrder);

  const sections = [
    ...pinnedFolders.map(folderSection),
    ...input.teams.map(teamSection),
    ...otherFolders.map(folderSection),
  ];

  const filed = new Set(input.folders.flatMap((f) => f.threadIds));
  const loose = input.threads.filter((t) => !teamed.has(t.id) && !filed.has(t.id));

  return { sections, loose };
}

/**
 * Thread ids in rendered order (Shift+Click ranges, arrow-key navigation):
 * each expanded section's rows, then the loose tail as the caller sorted or
 * date-grouped it. Collapsed sections contribute nothing, matching the DOM.
 */
export function visibleThreadIds(sections: ThreadSection[], looseInOrder: Thread[]): string[] {
  const ids: string[] = [];
  for (const section of sections) {
    if (section.group.collapsed) continue;
    for (const thread of section.threads) ids.push(thread.id);
  }
  for (const thread of looseInOrder) ids.push(thread.id);
  return ids;
}
