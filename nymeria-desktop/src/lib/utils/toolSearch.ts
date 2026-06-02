export interface ToolSearchFields {
  name?: string;
  id?: string;
  shortName?: string;
  description?: string;
  category?: string;
  tags?: string[];
  toolType?: string;
  implementationType?: string | null;
}

export type ToolSearchFieldGetter<T> = (item: T) => ToolSearchFields;

export interface RankedTool<T> {
  item: T;
  score: number;
}

const WORD_RE = /[a-z0-9]+/g;

// Minimum score for a tool to count as a match. Kept low because the scoring
// below is conservative: a real lexical or token match clears this comfortably,
// while incidental bigram overlap does not.
const SCORE_FLOOR = 0.3;

// Relative weight per field. Name-like fields dominate; descriptions count for
// less so a stray word in a long description never outranks a name hit.
const WEIGHT_NAME = 1;
const WEIGHT_CATEGORY = 0.85;
const WEIGHT_TAGS = 0.85;
const WEIGHT_DESCRIPTION = 0.6;

export function defaultToolSearchFields(item: Record<string, unknown>): ToolSearchFields {
  return {
    name: stringValue(item.name),
    id: stringValue(item.id),
    shortName: stringValue(item.shortName ?? item.short_name),
    description: stringValue(item.description),
    category: stringValue(item.category),
    tags: Array.isArray(item.tags) ? item.tags.map(String) : [],
    toolType: stringValue(item.toolType ?? item.tool_type),
    implementationType: stringValue(item.implementationType ?? item.implementation_type)
  };
}

export function rankToolSearch<T>(
  items: T[],
  query: string,
  getFields: ToolSearchFieldGetter<T> = (item) =>
    defaultToolSearchFields(item as Record<string, unknown>)
): RankedTool<T>[] {
  const queryJoined = joined(query);
  const queryTokens = tokens(query);
  if (!queryJoined && queryTokens.length === 0) {
    return items.map((item) => ({ item, score: 1 }));
  }

  return items
    .map((item) => {
      const fields = getFields(item);
      return { item, fields, score: scoreFields(fields, queryJoined, queryTokens) };
    })
    .filter((entry) => entry.score >= SCORE_FLOOR)
    .sort((a, b) => b.score - a.score || toolName(a.fields).localeCompare(toolName(b.fields)))
    .map((entry) => ({ item: entry.item, score: entry.score }));
}

export function filterToolSearch<T>(
  items: T[],
  query: string,
  getFields?: ToolSearchFieldGetter<T>
): T[] {
  return rankToolSearch(items, query, getFields).map((entry) => entry.item);
}

function scoreFields(fields: ToolSearchFields, queryJoined: string, queryTokens: string[]): number {
  let best = 0;
  const consider = (value: string | null | undefined, weight: number) => {
    if (!value) return;
    const quality = fieldQuality(value, queryJoined, queryTokens);
    if (quality > 0) best = Math.max(best, quality * weight);
  };

  consider(fields.name, WEIGHT_NAME);
  consider(fields.id, WEIGHT_NAME);
  consider(fields.shortName, WEIGHT_NAME);
  consider(fields.category, WEIGHT_CATEGORY);
  consider(fields.description, WEIGHT_DESCRIPTION);

  const tagText = [...(fields.tags ?? []), fields.toolType, fields.implementationType]
    .filter(Boolean)
    .join(' ');
  consider(tagText, WEIGHT_TAGS);

  return best;
}

// Match quality of one field against the query, in [0, 1]. Combines a
// contiguous lexical match (delimiter-insensitive) with an any-order token
// match, taking whichever is stronger. Tokenizes the field once and reuses it
// for both lanes.
function fieldQuality(value: string, queryJoined: string, queryTokens: string[]): number {
  const haystackTokens = tokens(value);
  if (haystackTokens.length === 0) return 0;

  // Fuzzy (typo / bigram) matching only makes sense for a single-word query.
  // For multi-word queries the token lane below is the authority; letting the
  // joined-form fuzzy paths fire would admit matches that ignore word
  // boundaries (e.g. "discord calendar" bigram-matching "discord_send_message").
  const allowFuzzy = queryTokens.length <= 1;
  let best = scoreText(queryJoined, haystackTokens.join(''), allowFuzzy);
  if (queryTokens.length > 0) {
    best = Math.max(best, tokenScore(queryTokens, haystackTokens));
  }
  return best;
}

// Contiguous match on the delimiter-stripped form: exact > prefix > substring >
// in-order subsequence, then (single-word queries only) whole-string typo >
// bigram similarity.
function scoreText(query: string, haystack: string, allowFuzzy: boolean): number {
  if (!query || !haystack) return 0;
  if (query === haystack) return 1;
  if (haystack.startsWith(query)) return 0.93;
  if (haystack.includes(query)) return 0.82;
  if (isSubsequence(query, haystack)) {
    return Math.max(0.4, 0.68 - Math.max(0, haystack.length - query.length) / 80);
  }
  if (!allowFuzzy) return 0;
  if (query.length >= 4) {
    const dist = boundedLevenshtein(query, haystack, 2);
    if (dist >= 0) return Math.max(0, 0.72 - dist * 0.12);
  }
  return diceCoefficient(query, haystack);
}

// Any-order word match: every query token must hit some field token. Returns 0
// if any query token has no match (AND semantics), else the mean per-token
// quality, scaled just under a perfect contiguous match.
function tokenScore(queryTokens: string[], haystackTokens: string[]): number {
  if (queryTokens.length === 0 || haystackTokens.length === 0) return 0;
  let total = 0;
  for (const qt of queryTokens) {
    let bestForToken = 0;
    for (const ht of haystackTokens) {
      bestForToken = Math.max(bestForToken, tokenPairScore(qt, ht));
      if (bestForToken === 1) break;
    }
    if (bestForToken === 0) return 0;
    total += bestForToken;
  }
  return (total / queryTokens.length) * 0.96;
}

function tokenPairScore(query: string, token: string): number {
  if (query === token) return 1;
  if (token.startsWith(query)) return 0.9;
  if (query.startsWith(token)) return 0.78;
  if (query.length >= 3 && token.includes(query)) return 0.7;
  if (query.length >= 4 && boundedLevenshtein(query, token, 1) === 1) return 0.66;
  return 0;
}

function diceCoefficient(a: string, b: string): number {
  if (a.length < 2 || b.length < 2) return a === b ? 1 : 0;
  const aPairs = bigrams(a);
  const bPairs = bigrams(b);
  let overlap = 0;
  const counts = new Map<string, number>();
  for (const pair of bPairs) counts.set(pair, (counts.get(pair) ?? 0) + 1);
  for (const pair of aPairs) {
    const count = counts.get(pair) ?? 0;
    if (count > 0) {
      overlap += 1;
      counts.set(pair, count - 1);
    }
  }
  return (2 * overlap) / (aPairs.length + bPairs.length);
}

function bigrams(value: string): string[] {
  const pairs: string[] = [];
  for (let i = 0; i < value.length - 1; i += 1) {
    pairs.push(value.slice(i, i + 2));
  }
  return pairs;
}

function isSubsequence(needle: string, haystack: string): boolean {
  let index = 0;
  for (const char of haystack) {
    if (char === needle[index]) {
      index += 1;
      if (index === needle.length) return true;
    }
  }
  return false;
}

// Levenshtein distance with an early bail-out: returns -1 as soon as the
// distance is known to exceed `max`, so callers pay only for near-matches.
function boundedLevenshtein(a: string, b: string, max: number): number {
  const al = a.length;
  const bl = b.length;
  if (Math.abs(al - bl) > max) return -1;
  let prev = new Array<number>(bl + 1);
  let curr = new Array<number>(bl + 1);
  for (let j = 0; j <= bl; j += 1) prev[j] = j;
  for (let i = 1; i <= al; i += 1) {
    curr[0] = i;
    let rowMin = curr[0];
    for (let j = 1; j <= bl; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      if (curr[j] < rowMin) rowMin = curr[j];
    }
    if (rowMin > max) return -1;
    const tmp = prev;
    prev = curr;
    curr = tmp;
  }
  return prev[bl] <= max ? prev[bl] : -1;
}

// Delimiter-stripped, lowercased form for contiguous matching: "send_email" and
// "sendEmail" both collapse to "sendemail".
function joined(value: unknown): string {
  return tokens(String(value ?? '')).join('');
}

// Word tokens, splitting on camelCase boundaries and any non-alphanumeric run.
function tokens(value: string): string[] {
  const spaced = String(value ?? '').replace(/([a-z0-9])([A-Z])/g, '$1 $2');
  return spaced.toLowerCase().match(WORD_RE) ?? [];
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function toolName(fields: ToolSearchFields): string {
  return fields.name || fields.id || fields.shortName || '';
}
