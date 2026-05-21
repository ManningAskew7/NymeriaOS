export interface EffectiveToolCountsInput {
  defaultToolNames: readonly string[];
  enabledTools?: readonly string[] | null;
  disabledTools?: readonly string[] | null;
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
}: EffectiveToolCountsInput): EffectiveToolCounts {
  const defaultSet = new Set(defaultToolNames.filter(Boolean));
  const enabledSet = new Set((enabledTools ?? []).filter(Boolean));
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
