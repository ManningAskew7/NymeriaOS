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

  it('prefixes a named target with the verb', () => {
    expect(getToolSummary('trigger_config', { action: 'create', name: 'daily digest' })).toBe(
      'create daily digest',
    );
  });

  it('uses a named target without a verb', () => {
    expect(getToolSummary('memory_read', { scope: 'global', key: 'coffee_pref' })).toBe(
      'coffee_pref',
    );
  });

  it('combines a verb with an explicit entity type', () => {
    expect(getToolSummary('servicenow_manage', { action: 'create', table: 'incident' })).toBe(
      'create incident',
    );
  });

  it('derives the entity type from an entity id argument, ignoring plumbing ids', () => {
    expect(
      getToolSummary('crm_update', { action: 'update', contact_id: 'x1', account_id: 'acc9' }),
    ).toBe('update contact');
  });

  it('uses an entity type alone when there is no verb', () => {
    expect(getToolSummary('servicenow_list_records', { table: 'incident' })).toBe('incident');
  });

  it('combines a bare verb with the noun implied by the tool name', () => {
    expect(getToolSummary('trigger_info', { action: 'list' })).toBe('list trigger');
  });

  it('uses a standalone qualifier facet', () => {
    expect(getToolSummary('memory_read', { scope: 'global' })).toBe('global');
    expect(getToolSummary('nym_todo_list', { filter_status: 'done' })).toBe('done');
  });

  it('suppresses a label that only restates the tool name', () => {
    // verb "list" + name noun "triggers" == the tool name itself.
    expect(getToolSummary('list_triggers', { action: 'list' })).toBeNull();
  });

  it('no longer uses the static description and returns null without specifics', () => {
    expect(getToolSummary('memory_read', {}, 'Read memory. Get a specific entry.')).toBeNull();
    expect(
      getToolSummary('some_tool', { fields_json: '{"a":1}', limit: 5 }, 'Create a record.'),
    ).toBeNull();
  });

  it('returns null when nothing is descriptive', () => {
    expect(getToolSummary('some_tool', { fields_json: '{}' })).toBeNull();
    expect(getToolSummary('some_tool', {})).toBeNull();
  });

  it('ignores object-valued arguments rather than dumping JSON', () => {
    expect(getToolSummary('some_tool', { message: { nested: 1 } })).toBeNull();
  });

  it('handles missing or undefined args', () => {
    expect(getToolSummary('web_search', undefined)).toBeNull();
  });

  it('keeps a long command at length, bounded only by the content safety cap', () => {
    const long = 'echo ' + 'word '.repeat(200).trim();
    const result = getToolSummary('bash_execute', { command: `${long}` });
    expect(result).not.toBeNull();
    // Content args are not word-capped; they keep length up to MAX_CONTENT_LEN.
    expect(result!.length).toBeGreaterThan(80);
    expect(result!.length).toBeLessThanOrEqual(160);
    expect(result!.endsWith('…')).toBe(true);
  });

  it('does not word-cap a multi-word command', () => {
    expect(
      getToolSummary('bash_execute', { command: 'docker logs nymeria-api --tail 50 | grep error' }),
    ).toBe('docker logs nymeria-api --tail 50 | grep error');
  });
});
