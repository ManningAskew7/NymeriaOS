/**
 * Slash-command palette ranking, shared by both apps' composers.
 *
 * Tier matches so the palette surfaces what the user is most likely typing
 * first: prefix-on-name > substring-on-name > description-only. Without
 * tiers, a permissive description-match floods the list with commands whose
 * descriptions happen to contain a common letter (e.g. "h" pulls in any
 * command mentioning "the" or "thread"), and the backend's (category, name)
 * sort then surfaces alphabetically early categories like "Goals" at the top
 * regardless of relevance.
 *
 * Deliberately NOT `toolSearch.ts::filterToolSearch`: the palette wants
 * deterministic tiers that preserve the backend's (category, name) order
 * within each tier, no fuzzy/subsequence lane admitting loose matches, and
 * no alphabetical re-sort. toolSearch stays the answer for scored typeahead
 * over tool panels.
 */

import type { SlashCommandInfo } from '$lib/types';

/** Rank `commands` against `query` (matched case-insensitively; an empty
 *  query returns the full catalog in backend order). */
export function filterCommands(
  commands: SlashCommandInfo[],
  query: string
): SlashCommandInfo[] {
  const q = query.toLowerCase();
  if (!q) return commands;
  const prefix: SlashCommandInfo[] = [];
  const nameSub: SlashCommandInfo[] = [];
  const descOnly: SlashCommandInfo[] = [];
  for (const cmd of commands) {
    const name = cmd.name.toLowerCase();
    if (name.startsWith(q)) {
      prefix.push(cmd);
    } else if (name.includes(q)) {
      nameSub.push(cmd);
    } else if (cmd.description.toLowerCase().includes(q)) {
      descOnly.push(cmd);
    }
  }
  return [...prefix, ...nameSub, ...descOnly];
}
