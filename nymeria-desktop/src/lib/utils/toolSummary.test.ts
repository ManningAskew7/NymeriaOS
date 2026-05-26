import { describe, expect, it } from 'vitest';

import { getToolSummary } from './toolSummary';

describe('getToolSummary', () => {
  it('uses a self-describing query argument', () => {
    expect(getToolSummary('web_search', { query: 'best coffee shops in sydney' })).toBe(
      'best coffee shops in sydney',
    );
  });

  it('uses the command argument for bash', () => {
    expect(getToolSummary('bash_execute', { command: 'git status' })).toBe('git status');
  });

  it('uses the task argument for callable-thread agents', () => {
    expect(getToolSummary('OutlookAgent', { task: 'fetch unread emails', mode: 'ask' })).toBe(
      'fetch unread emails',
    );
  });

  it('joins array values like queries', () => {
    expect(getToolSummary('web_search', { queries: ['a', 'b', 'c', 'd'] })).toBe('a, b, c');
  });

  it('uses discriminator arguments for multipurpose tools', () => {
    expect(getToolSummary('servicenow_list_records', { table: 'incident' })).toBe('incident');
    expect(getToolSummary('trigger_config', { action: 'create', name: 'x' })).toBe('create');
  });

  it('prefers a self-describing argument over the description fallback', () => {
    expect(
      getToolSummary('web_search', { query: 'tea' }, 'Search the web for current information.'),
    ).toBe('tea');
  });

  it('falls back to the description when no argument is descriptive', () => {
    expect(
      getToolSummary('some_tool', { fields_json: '{"a":1}', limit: 5 }, 'Create a record.'),
    ).toBe('Create a record.');
  });

  it('returns null when nothing is descriptive and no description is given', () => {
    expect(getToolSummary('some_tool', { fields_json: '{}' })).toBeNull();
    expect(getToolSummary('some_tool', {})).toBeNull();
  });

  it('ignores object-valued arguments rather than dumping JSON', () => {
    expect(getToolSummary('some_tool', { message: { nested: 1 } })).toBeNull();
  });

  it('handles missing or undefined args', () => {
    expect(getToolSummary('web_search', undefined)).toBeNull();
  });

  it('collapses whitespace and truncates long values', () => {
    const long = 'a '.repeat(200).trim();
    const result = getToolSummary('bash_execute', { command: `echo\n\n${long}` });
    expect(result).not.toBeNull();
    // Matches the MAX_LEN cap in toolSummary.ts.
    expect(result!.length).toBeLessThanOrEqual(120);
    expect(result!.endsWith('…')).toBe(true);
  });
});
