import { describe, expect, it } from 'vitest';

import { filterCommands } from './commandSearch';
import type { SlashCommandInfo } from '$lib/types';

function cmd(name: string, description = ''): SlashCommandInfo {
  return {
    name,
    description,
    usage: `/${name}`,
    category: 'Test',
    subcommands: [],
    id: name.replace(/\s+/g, '.'),
    path: name.split(' '),
    aliases: [],
    scope: 'global',
    surfaces: [],
    agent_allowed: true,
    requires_thread: false,
    requires_admin: false,
    mutates_state: false,
    danger_level: 'safe',
    execution_kind: 'command',
  };
}

describe('commandSearch: tiered palette ranking (backlog #135)', () => {
  it('ranks prefix-on-name above substring-on-name above description-only', () => {
    const catalog = [
      cmd('model', 'Switch the thread model'),   // description-only for "th"
      cmd('other', 'No match tier: name substring'), // name-substring for "th"
      cmd('think', 'Reasoning effort'),          // name-prefix for "th"
    ];

    const ranked = filterCommands(catalog, 'th');

    expect(ranked.map((c) => c.name)).toEqual(['think', 'other', 'model']);
  });

  it('matches case-insensitively on both name and query (the mobile palette bug)', () => {
    // Backend names are lowercase today, but nothing should depend on it.
    expect(filterCommands([cmd('Think')], 'th').map((c) => c.name)).toEqual(['Think']);
    expect(filterCommands([cmd('think')], 'TH').map((c) => c.name)).toEqual(['think']);
    expect(filterCommands([cmd('model', 'Switch the ACTIVE model')], 'active')).toHaveLength(1);
  });

  it('returns the full catalog in backend order for an empty query', () => {
    const catalog = [cmd('zeta'), cmd('alpha')];
    expect(filterCommands(catalog, '')).toEqual(catalog);
  });

  it('drops commands matching in no tier', () => {
    expect(filterCommands([cmd('model', 'Switch models')], 'hook')).toEqual([]);
  });
});
