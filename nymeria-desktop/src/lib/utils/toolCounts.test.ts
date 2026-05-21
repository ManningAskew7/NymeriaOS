import { describe, expect, it } from 'vitest';

import { computeEffectiveToolCounts } from './toolCounts';

describe('computeEffectiveToolCounts', () => {
  it('counts default tools as active', () => {
    const counts = computeEffectiveToolCounts({
      defaultToolNames: ['file_read', 'web_search'],
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
});
