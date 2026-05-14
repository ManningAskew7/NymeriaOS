export interface ToolSearchFields {
  name?: string;
  id?: string;
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

export function defaultToolSearchFields(item: Record<string, unknown>): ToolSearchFields {
  return {
    name: stringValue(item.name),
    id: stringValue(item.id),
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
  const normalizedQuery = normalize(query);
  if (!normalizedQuery) {
    return items.map((item) => ({ item, score: 1 }));
  }

  return items
    .map((item) => ({ item, score: scoreFields(getFields(item), normalizedQuery) }))
    .filter((entry) => entry.score >= 0.34)
    .sort((a, b) => b.score - a.score || toolName(getFields(a.item)).localeCompare(toolName(getFields(b.item))));
}

export function filterToolSearch<T>(
  items: T[],
  query: string,
  getFields?: ToolSearchFieldGetter<T>
): T[] {
  return rankToolSearch(items, query, getFields).map((entry) => entry.item);
}

function scoreFields(fields: ToolSearchFields, query: string): number {
  const name = normalize(fields.name);
  const id = normalize(fields.id);
  const category = normalize(fields.category);
  const description = normalize(fields.description);
  const tagText = normalize([...(fields.tags ?? []), fields.toolType, fields.implementationType].join(' '));
  const snakeName = normalize(splitSnake(fields.name ?? fields.id ?? ''));
  const haystacks = [name, id, snakeName, category, tagText, description].filter(Boolean);

  let best = 0;
  for (const haystack of haystacks) {
    best = Math.max(best, scoreText(query, haystack));
  }

  const queryTokens = tokens(query);
  const combinedTokens = new Set(tokens(haystacks.join(' ')));
  if (queryTokens.length > 0 && queryTokens.every((token) => combinedTokens.has(token))) {
    best = Math.max(best, 0.78);
  }

  return best;
}

function scoreText(query: string, haystack: string): number {
  if (!query || !haystack) return 0;
  if (query === haystack) return 1;
  if (haystack.startsWith(query)) return 0.92;
  if (haystack.includes(query)) return 0.82;
  if (isSubsequence(query, haystack)) {
    return Math.max(0.42, 0.74 - Math.max(0, haystack.length - query.length) / 80);
  }
  return diceCoefficient(query, haystack);
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

function normalize(value: unknown): string {
  return tokens(String(value ?? '')).join('');
}

function tokens(value: string): string[] {
  return (splitSnake(value).toLowerCase().match(WORD_RE) ?? []).filter(Boolean);
}

function splitSnake(value: string): string {
  return value.replace(/[_\-.]+/g, ' ');
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function toolName(fields: ToolSearchFields): string {
  return fields.name || fields.id || '';
}
