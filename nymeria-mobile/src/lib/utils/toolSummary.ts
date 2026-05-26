/**
 * Deterministic tool-call describer.
 *
 * Produces a short, plain-English label for a tool call, shown next to the tool
 * name on collapsed tool cards so users can follow what the agent is doing
 * without expanding each card. No LLM is involved.
 *
 * Inputs are only the tool name, its arguments, and (optionally) the tool's
 * short description routed from the backend. It never reads any store.
 *
 * Returns null when no useful label can be derived; the card then renders the
 * raw tool name only, exactly as before.
 *
 * NOTE: this file is kept byte-identical between nymeria-desktop and
 * nymeria-mobile and is registered in scripts/check_cross_app_drift.py.
 */

// Arguments whose value is itself a good description of the action. Ordered by
// preference; the first present, non-empty value wins.
const SELF_DESCRIBING_ARGS = [
  'task',
  'command',
  'query',
  'queries',
  'q',
  'prompt',
  'question',
  'message',
  'text',
  'expression',
  'soql',
  'code',
  'url',
  'file_path',
  'path',
  'pattern',
];

// Discriminator arguments that say what a multipurpose tool is doing. Verb-like
// keys first, then the entity they act on.
const DISCRIMINATOR_ARGS = [
  'action',
  'operation',
  'resource',
  'table',
  'model',
  'object_type',
  'object_name',
  'record_type',
];

const MAX_LEN = 120;

function coerceScalar(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((v) => coerceScalar(v))
      .filter((v): v is string => v !== null);
    return parts.length > 0 ? parts.slice(0, 3).join(', ') : null;
  }
  // Objects / nested structures do not make a good bare label.
  return null;
}

function clean(text: string): string {
  // Collapse whitespace and newlines, then cap length so one card line stays
  // tidy. The card CSS also ellipsis-truncates at the available width; this cap
  // is a safety bound against very long argument values.
  const collapsed = text.replace(/\s+/g, ' ').trim();
  if (collapsed.length <= MAX_LEN) return collapsed;
  return collapsed.slice(0, MAX_LEN - 1).trimEnd() + '…';
}

export function getToolSummary(
  name: string,
  args: Record<string, unknown> | undefined,
  description?: string,
): string | null {
  const safeArgs = args ?? {};

  // 1. Self-describing argument: the value is the description.
  for (const key of SELF_DESCRIBING_ARGS) {
    if (key in safeArgs) {
      const value = coerceScalar(safeArgs[key]);
      if (value) return clean(value);
    }
  }

  // 2. Discriminator(s): what a multipurpose tool is doing.
  const discriminatorParts: string[] = [];
  for (const key of DISCRIMINATOR_ARGS) {
    if (key in safeArgs) {
      const value = coerceScalar(safeArgs[key]);
      if (value) discriminatorParts.push(value);
    }
  }
  if (discriminatorParts.length > 0) {
    return clean(discriminatorParts.join(' '));
  }

  // 3. Fallback: the tool's short description (routed from the backend).
  if (description) {
    const value = coerceScalar(description);
    if (value) return clean(value);
  }

  // 4. Nothing useful to add: render the raw name only.
  return null;
}
