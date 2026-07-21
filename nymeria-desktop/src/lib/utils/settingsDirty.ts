// Field-level dirty comparison for the settings panels' per-tab unsaved-change
// tracking (nav badges + disabled-until-dirty footer saves).
//
// Baselines are snapshots of the same edit-model values the form fields bind
// to, captured right after load and after each successful save, so comparison
// never needs to re-derive server-to-form transforms.

function normalize(value: unknown): unknown {
  if (value == null) return null;
  if (typeof value === 'string') return value.trim();
  return value;
}

export function fieldChanged(current: unknown, baseline: unknown): boolean {
  const a = normalize(current);
  const b = normalize(baseline);
  if (a === null && b === null) return false;
  if (typeof a === 'number' && typeof b === 'number' && Number.isNaN(a) && Number.isNaN(b)) {
    return false;
  }
  if (a === b) return false;
  // Structured values (arrays, plain objects) compare by shape.
  if (typeof a === 'object' && typeof b === 'object' && a !== null && b !== null) {
    return JSON.stringify(a) !== JSON.stringify(b);
  }
  return true;
}

/**
 * Count fields in `current` whose value differs from `baseline`.
 * A missing baseline (tab not loaded yet) counts as clean: dirtiness only
 * exists relative to a known loaded state.
 */
export function countChangedFields(
  current: Record<string, unknown>,
  baseline: Record<string, unknown> | undefined
): number {
  if (!baseline) return 0;
  let changed = 0;
  for (const key of Object.keys(current)) {
    if (fieldChanged(current[key], baseline[key])) changed += 1;
  }
  return changed;
}
