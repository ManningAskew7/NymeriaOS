export interface EffectiveToolCountsInput {
  defaultToolNames: readonly string[];
  enabledTools?: readonly string[] | null;
  disabledTools?: readonly string[] | null;
  /**
   * Live (non-expired) TTL'd tool names — e.g. Skill Kit required_tools bound
   * with a TTL. These live in ThreadConfig.temporaryTools, NOT enabledTools,
   * but are active on the thread exactly like enabled tools, so they must be
   * counted. Use {@link liveTemporaryToolNames} to derive this from the raw map.
   */
  temporaryTools?: readonly string[] | null;
}

/**
 * Extract the names of TTL'd tools that have not yet expired, mirroring the
 * backend's graph-build liveness check (expires_at in the future). Entries
 * with a missing/unparseable expiry are treated as live (fail-open), matching
 * the backend which keeps anything it cannot positively expire.
 */
export function liveTemporaryToolNames(
  temporaryTools?: Record<string, { expiresAt?: string | null }> | null,
  nowMs: number = Date.now()
): string[] {
  if (!temporaryTools) return [];
  const live: string[] = [];
  for (const [name, entry] of Object.entries(temporaryTools)) {
    const expires = entry?.expiresAt;
    if (!expires) {
      live.push(name);
      continue;
    }
    const ts = Date.parse(expires);
    if (Number.isNaN(ts) || ts > nowMs) {
      live.push(name);
    }
  }
  return live;
}

export interface EffectiveToolCounts {
  activeNames: Set<string>;
  activeNonMcpCount: number;
  activeMcpCount: number;
  totalActiveCount: number;
  disabledNonMcpCount: number;
  disabledMcpCount: number;
  enabledExtraNonMcpCount: number;
  enabledExtraMcpCount: number;
}

export function isMcpToolName(name: string): boolean {
  return name.startsWith('mcp__');
}

export function computeEffectiveToolCounts({
  defaultToolNames,
  enabledTools = [],
  disabledTools = [],
  temporaryTools = [],
}: EffectiveToolCountsInput): EffectiveToolCounts {
  const defaultSet = new Set(defaultToolNames.filter(Boolean));
  // TTL'd tools are active extras just like enabledTools — merge them so they
  // count toward the active set and the "optional enabled" breakdown.
  const enabledSet = new Set(
    [...(enabledTools ?? []), ...(temporaryTools ?? [])].filter(Boolean)
  );
  const disabledSet = new Set((disabledTools ?? []).filter(Boolean));

  const activeNames = new Set<string>(defaultSet);
  for (const name of enabledSet) {
    activeNames.add(name);
  }
  for (const name of disabledSet) {
    activeNames.delete(name);
  }

  const enabledExtraNames = [...enabledSet].filter(
    (name) => !defaultSet.has(name) && !disabledSet.has(name)
  );

  return {
    activeNames,
    activeNonMcpCount: countWhere(activeNames, (name) => !isMcpToolName(name)),
    activeMcpCount: countWhere(activeNames, isMcpToolName),
    totalActiveCount: activeNames.size,
    disabledNonMcpCount: countWhere(disabledSet, (name) => !isMcpToolName(name)),
    disabledMcpCount: countWhere(disabledSet, isMcpToolName),
    enabledExtraNonMcpCount: enabledExtraNames.filter((name) => !isMcpToolName(name)).length,
    enabledExtraMcpCount: enabledExtraNames.filter(isMcpToolName).length,
  };
}

function countWhere(values: Iterable<string>, predicate: (name: string) => boolean): number {
  let count = 0;
  for (const value of values) {
    if (predicate(value)) count += 1;
  }
  return count;
}
