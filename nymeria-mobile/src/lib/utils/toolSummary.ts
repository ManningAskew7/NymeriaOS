/**
 * Deterministic tool-call describer.
 *
 * Produces a short, plain-English label for a tool call, shown next to the tool
 * name on collapsed tool cards so users can follow what the agent is doing
 * without expanding each card. No LLM is involved.
 *
 * The guiding principle: the label describes the SPECIFICS of this call (the
 * data in its arguments), not the tool's general purpose. The tool name is
 * already shown right beside the label, so a label that merely restates the
 * name ("memory_read" -> "read memory") adds nothing and is suppressed.
 *
 * Derivation, most-specific first:
 *   1. A content argument whose value is the gist of the call (search query,
 *      shell command, agent task, file path...). The value is the label.
 *   2. A named target the call acts on (name, title, subject, key...), prefixed
 *      by the verb when the tool takes an action/operation argument.
 *   3. A verb plus the entity type it acts on, taken from an entity argument
 *      (object_type, table...) or the type implied by an entity id argument
 *      (contact_id -> "contact"). Opaque plumbing ids are ignored.
 *   4. A verb alone, combined with the noun implied by the tool name so it
 *      reads naturally ("list" on trigger_info -> "list trigger").
 *   5. A standalone qualifier facet (scope, status, mode...).
 *   6. An entity type alone.
 * Returns null when none of these add anything beyond the tool name; the card
 * then renders the raw tool name only.
 *
 * The `description` parameter (the tool's static short description) is no longer
 * used to derive labels: being identical for every call, it can only paraphrase
 * the tool's purpose, never describe a specific invocation. It is kept in the
 * signature so existing call sites and the backend plumbing need not change.
 *
 * NOTE: this file is kept byte-identical between nymeria-desktop and
 * nymeria-mobile and is registered in scripts/check_cross_app_drift.py.
 */

// 1. Arguments whose VALUE is the gist of the call. First present value wins.
const CONTENT_ARGS = [
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

// 2. Arguments whose VALUE names the specific target the call acts on.
const TARGET_ARGS = [
  'name',
  'title',
  'subject',
  'key',
  'label',
  'to',
  'recipient',
  'channel',
];

// 3. Verb arguments: what operation a multipurpose tool performs.
const VERB_ARGS = ['action', 'operation', 'method'];

// 4. Entity-type arguments: what kind of object the call acts on.
const ENTITY_ARGS = [
  'object_type',
  'object_name',
  'record_type',
  'resource',
  'table',
  'sobject',
];

// 5. Standalone qualifier facets: a single descriptive aspect of the call.
const QUALIFIER_ARGS = ['scope', 'filter_status', 'status', 'mode', 'category', 'kind'];

// Opaque identifier arguments that carry no human-readable meaning. Never used
// to derive an entity type from a `*_id` argument name.
const PLUMBING_ID_ARGS = new Set(['user_id', 'account_id', 'thread_id', 'tool_call_id']);

// Generic verb/structural words stripped when deriving a noun from a tool name,
// so the remaining tokens are the entity the tool acts on.
const NAME_NOISE_WORDS = new Set([
  'info',
  'manage',
  'read',
  'write',
  'list',
  'get',
  'set',
  'fetch',
  'create',
  'update',
  'delete',
  'remove',
  'search',
  'query',
  'find',
  'send',
  'run',
  'exec',
  'execute',
  'call',
  'tool',
  'tools',
  'api',
  'nym',
  'do',
  'handle',
  'process',
  'make',
  'new',
  'add',
  'edit',
  'view',
  'show',
  'detail',
  'details',
  'data',
]);

// Filler words ignored when testing whether a label only restates the tool
// name. Kept to true stopwords: a verb such as "list" or "create" is meaningful
// in a label even though it is noise inside a tool name.
const STOPWORDS = new Set(['a', 'an', 'the', 'of', 'for', 'to', 'and', 'or', 'in', 'on', 'with', 'by']);

const MAX_WORDS = 6;
// Composed verb+object labels are short by nature, so cap them tightly. Content
// argument values (commands, queries, paths) keep their length and let the card
// width ellipsize them; the char bound is only a DOM-size safety net.
const MAX_LABEL_LEN = 80;
const MAX_CONTENT_LEN = 160;

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

function humanize(token: string): string {
  return token.replace(/_/g, ' ').trim().toLowerCase();
}

function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 0);
}

// A label that only restates the tool name adds nothing beside the name.
function restatesName(label: string, name: string): boolean {
  const nameTokens = new Set(tokenize(name));
  const significant = tokenize(label).filter((t) => !STOPWORDS.has(t));
  if (significant.length === 0) return true;
  return significant.every((t) => nameTokens.has(t));
}

// The noun implied by a tool name: its tokens minus generic verb/structural
// words. "trigger_info" -> "trigger", "list_calendar_events" -> "calendar events".
function nounFromName(name: string): string {
  const tokens = tokenize(name);
  const kept = tokens.filter((t) => !NAME_NOISE_WORDS.has(t));
  return (kept.length > 0 ? kept : tokens).join(' ');
}

function clean(text: string, capWords: boolean): string {
  // Collapse whitespace and newlines. Composed labels are trimmed to a short
  // phrase (word + char bound); content values keep their length and rely on
  // the card's width ellipsis, bounded only as a DOM-size safety net.
  const collapsed = text.replace(/\s+/g, ' ').trim();
  let result = collapsed;
  let truncated = false;

  if (capWords) {
    const words = result.split(' ');
    if (words.length > MAX_WORDS) {
      result = words.slice(0, MAX_WORDS).join(' ');
      truncated = true;
    }
  }

  const maxLen = capWords ? MAX_LABEL_LEN : MAX_CONTENT_LEN;
  if (result.length > maxLen) {
    result = result.slice(0, maxLen - 1).trimEnd();
    truncated = true;
  }
  if (truncated) {
    // Drop trailing punctuation before the ellipsis so it reads cleanly.
    result = result.replace(/[\s.,;:!?]+$/, '') + '…';
  }
  return result;
}

// Apply the length caps and suppress labels that only restate the tool name.
// capWords is false for content values, which keep their length (see clean).
function finalize(label: string | null, name: string, capWords = true): string | null {
  if (!label) return null;
  const trimmed = clean(label, capWords);
  if (!trimmed || restatesName(trimmed, name)) return null;
  return trimmed;
}

function firstPresent(
  args: Record<string, unknown>,
  keys: string[],
): string | null {
  for (const key of keys) {
    if (key in args) {
      const value = coerceScalar(args[key]);
      if (value) return value;
    }
  }
  return null;
}

export function getToolSummary(
  name: string,
  args: Record<string, unknown> | undefined,
  description?: string,
): string | null {
  void description; // intentionally unused; see file header.
  const safeArgs = args ?? {};

  const verb = firstPresent(safeArgs, VERB_ARGS)?.toLowerCase() ?? null;

  // 1. Content argument: its value is the gist of the call. Kept at length; the
  //    card width ellipsizes it (no word cap).
  const content = firstPresent(safeArgs, CONTENT_ARGS);
  if (content) return finalize(content, name, false);

  // 2. Named target, prefixed by the verb when there is one.
  const target = firstPresent(safeArgs, TARGET_ARGS);
  if (target) return finalize(verb ? `${verb} ${target}` : target, name);

  // 3. Verb + entity type (explicit entity arg, else implied by a `*_id` arg).
  let entity = firstPresent(safeArgs, ENTITY_ARGS);
  if (entity) entity = humanize(entity);
  if (!entity) {
    for (const key of Object.keys(safeArgs)) {
      if (key.endsWith('_id') && !PLUMBING_ID_ARGS.has(key)) {
        const value = coerceScalar(safeArgs[key]);
        if (value) {
          entity = humanize(key.slice(0, -3));
          break;
        }
      }
    }
  }
  if (verb && entity) return finalize(`${verb} ${entity}`, name);

  // 4. Verb alone: combine with the noun implied by the tool name.
  if (verb) {
    const noun = nounFromName(name);
    return finalize(noun ? `${verb} ${noun}` : verb, name);
  }

  // 5. Standalone qualifier facet.
  const qualifier = firstPresent(safeArgs, QUALIFIER_ARGS);
  if (qualifier) return finalize(humanize(qualifier), name);

  // 6. Entity type alone.
  if (entity) return finalize(entity, name);

  // 7. Nothing specific to add: render the raw name only.
  return null;
}
