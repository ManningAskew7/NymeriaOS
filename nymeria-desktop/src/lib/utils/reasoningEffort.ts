/**
 * Reasoning-effort ladder shared by the global and per-thread LLM settings.
 *
 * Mirrors the backend ladder in nymeria/config/model_capabilities.py
 * (EFFORT_LEVELS). Unsupported levels are adjusted server-side onto the
 * model's supported ladder (over-asks drop to its ceiling), never rejected;
 * the UI warns so the clamp is not a surprise.
 */

export const REASONING_EFFORT_LEVELS: readonly string[] = [
  'off',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
];

const REASONING_EFFORT_LABELS: Record<string, string> = {
  off: 'Off',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max',
};

/** Human label for an effort level ("xhigh" renders as "Extra high"). */
export function reasoningEffortLabel(effort: string): string {
  return REASONING_EFFORT_LABELS[effort] ?? effort;
}

/**
 * True when the selected effort exceeds the model's highest supported level,
 * meaning the backend will reduce it. False when the selection is unset or
 * "off", when the model ceiling is missing, or when either value is unknown.
 */
export function effortExceedsModelMax(
  selected: string | null | undefined,
  maxEffort: string | null | undefined
): boolean {
  if (!selected || selected === 'off' || !maxEffort) return false;
  const selectedIdx = REASONING_EFFORT_LEVELS.indexOf(selected);
  const maxIdx = REASONING_EFFORT_LEVELS.indexOf(maxEffort);
  if (selectedIdx < 0 || maxIdx < 0) return false;
  return selectedIdx > maxIdx;
}
