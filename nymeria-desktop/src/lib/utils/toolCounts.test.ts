import { describe, expect, it } from 'vitest';

import { computeEffectiveToolCounts, liveTemporaryToolNames } from './toolCounts';

describe('computeEffectiveToolCounts', () => {
  it('counts default tools as active', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read', 'web_search_perplexity'],
    });

    expect(counts.totalActiveCount).toBe(2);
    expect(counts.activeNonMcpCount).toBe(2);
    expect(counts.activeMcpCount).toBe(0);
  });

  it('adds per-thread optional tools', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read'],
      enabledTools: ['calendar_list_events'],
    });

    expect(counts.totalActiveCount).toBe(2);
    expect(counts.enabledExtraNonMcpCount).toBe(1);
    expect(counts.activeNames.has('calendar_list_events')).toBe(true);
  });

  it('does not double-count a promoted optional tool still present in enabled_tools', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read', 'calendar_list_events'],
      enabledTools: ['calendar_list_events'],
    });

    expect(counts.totalActiveCount).toBe(2);
    expect(counts.enabledExtraNonMcpCount).toBe(0);
  });

  it('lets disabled_tools win over defaults and extras', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read', 'calendar_list_events'],
      enabledTools: ['calendar_list_events', 'browser_navigate'],
      disabledTools: ['calendar_list_events', 'browser_navigate'],
    });

    expect(counts.totalActiveCount).toBe(1);
    expect(counts.activeNames.has('calendar_list_events')).toBe(false);
    expect(counts.activeNames.has('browser_navigate')).toBe(false);
    expect(counts.disabledNonMcpCount).toBe(2);
    expect(counts.enabledExtraNonMcpCount).toBe(0);
  });

  it('splits MCP and non-MCP counts', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read', 'mcp__srv__search'],
      enabledTools: ['mcp__srv__lookup', 'calendar_list_events'],
      disabledTools: ['mcp__srv__search'],
    });

    expect(counts.activeNonMcpCount).toBe(2);
    expect(counts.activeMcpCount).toBe(1);
    expect(counts.enabledExtraMcpCount).toBe(1);
    expect(counts.disabledMcpCount).toBe(1);
  });

  it('counts live temporary (TTL) tools as active extras', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read'],
      enabledTools: [],
      // e.g. a Skill Kit's required_tools bound with a TTL
      temporaryTools: ['skill_write', 'tool_search'],
    });

    expect(counts.totalActiveCount).toBe(3);
    expect(counts.enabledExtraNonMcpCount).toBe(2);
    expect(counts.activeNames.has('skill_write')).toBe(true);
    expect(counts.activeNames.has('tool_search')).toBe(true);
  });

  it('lets disabled_tools win over a temporary tool', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read'],
      temporaryTools: ['skill_write'],
      disabledTools: ['skill_write'],
    });

    expect(counts.totalActiveCount).toBe(1);
    expect(counts.activeNames.has('skill_write')).toBe(false);
  });

  it('does not double-count a temporary tool also in enabled or default', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read'],
      enabledTools: ['tool_search'],
      temporaryTools: ['tool_search'],
    });

    expect(counts.totalActiveCount).toBe(2);
    expect(counts.enabledExtraNonMcpCount).toBe(1);
  });
});

describe('liveTemporaryToolNames', () => {
  const now = Date.parse('2026-05-27T12:00:00Z');

  it('returns names whose expiry is still in the future', () => {
    const live = liveTemporaryToolNames(
      {
        future_tool: { expiresAt: '2026-05-27T13:00:00Z' },
        past_tool: { expiresAt: '2026-05-27T11:00:00Z' },
      },
      now
    );

    expect(live).toEqual(['future_tool']);
  });

  it('treats missing or unparseable expiry as live (fail-open)', () => {
    const live = liveTemporaryToolNames(
      {
        no_expiry: { expiresAt: null },
        bad_expiry: { expiresAt: 'not-a-date' },
      },
      now
    );

    expect(live.sort()).toEqual(['bad_expiry', 'no_expiry']);
  });

  it('handles an empty or missing map', () => {
    expect(liveTemporaryToolNames(undefined, now)).toEqual([]);
    expect(liveTemporaryToolNames({}, now)).toEqual([]);
  });
});
