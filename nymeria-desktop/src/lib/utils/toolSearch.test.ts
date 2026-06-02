import { describe, expect, it } from 'vitest';

import { filterToolSearch, rankToolSearch } from './toolSearch';

type Item = {
  name?: string;
  shortName?: string;
  description?: string;
  category?: string;
  tags?: string[];
};

const names = (items: Item[]) => items.map((i) => i.name);

describe('rankToolSearch', () => {
  it('returns every item (score 1) for an empty or whitespace query', () => {
    const items: Item[] = [{ name: 'a_tool' }, { name: 'b_tool' }];
    expect(rankToolSearch(items, '')).toEqual([
      { item: items[0], score: 1 },
      { item: items[1], score: 1 },
    ]);
    expect(filterToolSearch(items, '   ')).toEqual(items);
  });

  it('matches multi-word queries regardless of word order', () => {
    const items: Item[] = [
      { name: 'gmail_send_email', description: 'Send an email via Gmail' },
      { name: 'team_email_sender', description: 'Send email to the team' },
      { name: 'read_file', description: 'Read a file from disk' },
    ];

    const forward = names(filterToolSearch(items, 'send email'));
    const reversed = names(filterToolSearch(items, 'email send'));

    expect(forward).toContain('gmail_send_email');
    expect(forward).toContain('team_email_sender');
    expect(forward).not.toContain('read_file');
    // Order of the words must not change which tools match.
    expect([...reversed].sort()).toEqual([...forward].sort());
  });

  it('orders results best-match-first (prefix beats substring)', () => {
    const items: Item[] = [{ name: 'read_file' }, { name: 'file_read' }];
    const ranked = filterToolSearch(items, 'file');
    expect(ranked[0].name).toBe('file_read'); // prefix match wins over substring
  });

  it('ranks a name match above a description-only match', () => {
    const items: Item[] = [
      { name: 'unrelated_tool', description: 'manage your calendar events' },
      { name: 'calendar_list', description: 'list things' },
    ];
    const ranked = filterToolSearch(items, 'calendar');
    expect(ranked[0].name).toBe('calendar_list');
  });

  it('tolerates a single-character typo', () => {
    const items: Item[] = [
      { name: 'github_search', description: 'Search GitHub repositories' },
      { name: 'gitlab_search', description: 'Search GitLab' },
    ];
    const matched = names(filterToolSearch(items, 'guthub'));
    expect(matched).toContain('github_search');
  });

  it('searches the MCP shortName, with exact beating prefix', () => {
    const items: Item[] = [
      { name: 'mcp__verylongserverid__send', shortName: 'send', description: 'Send a thing' },
      { name: 'mcp__otherserver__sendmail', shortName: 'sendmail', description: 'Send mail' },
    ];
    const ranked = filterToolSearch(items, 'send');
    expect(ranked[0].shortName).toBe('send'); // exact short-name match ranks first
    expect(names(ranked)).toContain('mcp__otherserver__sendmail');
  });

  it('does not bigram-match an unrelated tool for a multi-word query (AND semantics)', () => {
    const items: Item[] = [
      { name: 'discord_send_message', description: 'Send a message to Discord' },
      { name: 'create_calendar_event', description: 'Create a calendar event', category: 'calendar' },
    ];
    // No tool is both "discord" and "calendar"; the discord tool must not leak
    // in via fuzzy bigram overlap on the joined form.
    expect(names(filterToolSearch(items, 'discord calendar'))).not.toContain('discord_send_message');
  });

  it('matches a two-word query when both words are present', () => {
    const items: Item[] = [
      { name: 'create_calendar_event', description: 'Create a calendar event' },
      { name: 'discord_send_message', description: 'Send a message' },
    ];
    expect(names(filterToolSearch(items, 'calendar event'))).toContain('create_calendar_event');
  });

  it('drops items that do not match at all', () => {
    const items: Item[] = [{ name: 'read_file' }, { name: 'calendar_list' }];
    expect(filterToolSearch(items, 'zzzznomatch')).toHaveLength(0);
  });

  it('matches the underscore-delimited name when the query has no delimiter', () => {
    const items: Item[] = [{ name: 'web_search_perplexity' }];
    expect(names(filterToolSearch(items, 'websearch'))).toContain('web_search_perplexity');
  });
});
