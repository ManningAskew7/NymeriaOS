<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import Icon from '../common/Icon.svelte';
  import { CATEGORY_ORDER, getCategoryInfo, getGroupInfo } from '$lib/utils/toolCategories';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import type { GroupedToolItem } from '$lib/types';

  /**
   * Shared, collapsible, relevance-ordered grouped tool list. Owns all of the
   * grouping, nesting, collapse, render-on-expand and search-aware ordering for
   * a single pool of tools; each caller supplies normalized items plus a row
   * snippet so panel-specific controls (toggles, badges, edit buttons) stay in
   * the panel.
   *
   * Two modes:
   *  - 'category' (default): top-level by ToolCategory, rendered as readable
   *    cards. The Integrations category nests a second level
   *    (functional group -> service -> rows); every other category renders rows
   *    directly.
   *  - 'server': top-level by MCP server (one level of rows), for the MCP pane.
   *
   * Rows are rendered only for expanded groups, so a ~1,000-tool pool stays
   * cheap. When a search is active every group with matches auto-expands and
   * groups order best-match-first; with no query the curated category/group
   * order applies (A-Z within a leaf).
   */
  interface Props {
    /** Already filtered + relevance-ranked items for this pool. */
    items: GroupedToolItem[];
    searchActive: boolean;
    mode?: 'category' | 'server';
    /** Prefix for collapse ids so two pools never share open/closed state. */
    poolKey: string;
    /** Initial open state for top-level groups when no search is active. */
    defaultOpenTopLevel?: boolean;
    /** Transient collapse state, owned by the parent so it survives remounts. */
    expanded?: Set<string>;
    collapsed?: Set<string>;
    /** Server-mode only: installed servers that discovered zero tools. */
    emptyServers?: { id: string; name: string; enabled: boolean }[];
    /** Renders one row's inner content (info block + trailing controls). */
    row: Snippet<[GroupedToolItem]>;
  }

  let {
    items,
    searchActive,
    mode = 'category',
    poolKey,
    defaultOpenTopLevel = false,
    expanded = $bindable(new Set()),
    collapsed = $bindable(new Set()),
    emptyServers = [],
    row,
  }: Props = $props();

  // CATEGORY_INFO uses kebab-case Lucide names the local Icon set does not
  // define, so top-level category icons are mapped to valid names here.
  const CATEGORY_ICON: Record<string, string> = {
    general: 'terminal', profile: 'user', notepad: 'pin', self_modify: 'edit',
    todo: 'check', subagent: 'refresh', trigger: 'bolt', email: 'send',
    browser: 'folder', image: 'image', calendar: 'calendar', google_docs: 'fileText',
    integrations: 'server', skills: 'bolt', custom: 'tool', mcp_server: 'server',
  };
  const categoryIcon = (c: string) => CATEGORY_ICON[c] ?? 'tool';

  type GroupNode = {
    id: string;
    label: string;
    icon?: string;
    count: number;
    level: number;
    dormant?: boolean;
    children?: GroupNode[];
    rows?: GroupedToolItem[];
    emptyText?: string;
  };

  const sortByLabel = (arr: GroupedToolItem[]) =>
    [...arr].sort((a, b) => a.label.localeCompare(b.label));

  // Group an array by a key getter, preserving first-appearance order (which is
  // best-match-first when items arrive ranked).
  function groupBy(arr: GroupedToolItem[], keyOf: (i: GroupedToolItem) => string) {
    const map = new Map<string, GroupedToolItem[]>();
    for (const it of arr) {
      const k = keyOf(it);
      const bucket = map.get(k);
      if (bucket) bucket.push(it);
      else map.set(k, [it]);
    }
    return map;
  }

  function buildServiceNodes(arr: GroupedToolItem[], parentId: string): GroupNode[] {
    const byService = groupBy(arr, (i) => i.service || '_none');
    const entries = [...byService.entries()];
    if (!searchActive) {
      entries.sort((a, b) => {
        const la = a[1][0].serviceLabel || a[0];
        const lb = b[1][0].serviceLabel || b[0];
        return la.localeCompare(lb);
      });
    }
    return entries.map(([svc, svcItems]) => ({
      id: `${parentId}:svc:${svc}`,
      label: svcItems[0].serviceLabel || svc,
      count: svcItems.length,
      level: 2,
      rows: searchActive ? svcItems : sortByLabel(svcItems),
    }));
  }

  function buildIntegrationGroups(arr: GroupedToolItem[], parentId: string): GroupNode[] {
    const byGroup = groupBy(arr, (i) => i.group || '_ungrouped');
    const entries = [...byGroup.entries()];
    if (!searchActive) {
      entries.sort(
        (a, b) =>
          getGroupInfo(a[0], a[1][0].groupLabel ?? undefined).order -
          getGroupInfo(b[0], b[1][0].groupLabel ?? undefined).order,
      );
    }
    return entries.map(([grp, grpItems]) => {
      const info = getGroupInfo(grp, grpItems[0].groupLabel ?? undefined);
      return {
        id: `${parentId}:grp:${grp}`,
        label: grpItems[0].groupLabel || info.label,
        icon: info.icon,
        count: grpItems.length,
        level: 1,
        children: buildServiceNodes(grpItems, `${parentId}:grp:${grp}`),
      };
    });
  }

  function buildCategoryTree(arr: GroupedToolItem[]): GroupNode[] {
    const byCat = groupBy(arr, (i) => i.category);
    let cats: string[];
    if (searchActive) {
      cats = [...byCat.keys()];
    } else {
      const seen = new Set<string>();
      cats = [];
      for (const c of CATEGORY_ORDER) {
        if (byCat.has(c)) { cats.push(c); seen.add(c); }
      }
      for (const c of byCat.keys()) if (!seen.has(c)) cats.push(c);
    }
    return cats.map((cat) => {
      const catItems = byCat.get(cat)!;
      const id = `${poolKey}:cat:${cat}`;
      const node: GroupNode = {
        id,
        label: getCategoryInfo(cat).name,
        icon: categoryIcon(cat),
        count: catItems.length,
        level: 0,
      };
      if (cat === 'integrations') {
        node.children = buildIntegrationGroups(catItems, id);
      } else {
        node.rows = searchActive ? catItems : sortByLabel(catItems);
      }
      return node;
    });
  }

  function buildServerTree(arr: GroupedToolItem[]): GroupNode[] {
    const byServer = new Map<string, { name: string; enabled: boolean; items: GroupedToolItem[] }>();
    for (const it of arr) {
      const sid = it.serverId || '_unknown';
      let g = byServer.get(sid);
      if (!g) {
        g = { name: it.serverName || sid, enabled: it.serverEnabled ?? true, items: [] };
        byServer.set(sid, g);
      }
      g.items.push(it);
    }
    let entries = [...byServer.entries()];
    if (!searchActive) entries = entries.sort((a, b) => a[1].name.localeCompare(b[1].name));
    const nodes: GroupNode[] = entries.map(([sid, g]) => ({
      id: `${poolKey}:srv:${sid}`,
      label: g.name,
      icon: 'server',
      count: g.items.length,
      level: 0,
      dormant: !g.enabled,
      rows: searchActive ? g.items : sortByLabel(g.items),
    }));
    if (!searchActive) {
      for (const s of emptyServers) {
        nodes.push({
          id: `${poolKey}:srv:${s.id}`,
          label: s.name,
          icon: 'server',
          count: 0,
          level: 0,
          dormant: !s.enabled,
          rows: [],
          emptyText: 'No tools discovered. Rediscover in Settings -> MCP.',
        });
      }
    }
    return nodes;
  }

  const tree = $derived(mode === 'server' ? buildServerTree(items) : buildCategoryTree(items));

  const defaultOpenFor = (level: number) => (level === 0 ? defaultOpenTopLevel : false);

  function groupOpen(id: string, defaultOpen: boolean): boolean {
    if (searchActive) return true;
    if (collapsed.has(id)) return false;
    if (expanded.has(id)) return true;
    return defaultOpen;
  }

  function toggleGroup(id: string, defaultOpen: boolean) {
    const open = groupOpen(id, defaultOpen);
    const e = new Set(expanded);
    const c = new Set(collapsed);
    if (open) { c.add(id); e.delete(id); }
    else { e.add(id); c.delete(id); }
    expanded = e;
    collapsed = c;
  }
</script>

<div class="gl">
  {#each tree as node (node.id)}
    {@render groupNode(node, 0)}
  {/each}
</div>

{#snippet groupNode(node: GroupNode, level: number)}
  {@const open = groupOpen(node.id, defaultOpenFor(level))}
  <div class="gl-group" class:top={level === 0} class:dormant={node.dormant} style="--gl-level: {level}">
    <button
      class="gl-head"
      class:top={level === 0}
      class:open
      type="button"
      onclick={() => toggleGroup(node.id, defaultOpenFor(level))}
      aria-expanded={open}
    >
      <span class="gl-chevron" class:open><Icon name="chevronRight" size={13} /></span>
      {#if node.icon}<Icon name={node.icon} size={13} />{/if}
      <span class="gl-name">{node.label}</span>
      <span class="gl-count">{node.count}</span>
    </button>
    {#if open}
      <div class="gl-body" transition:slide={DROPDOWN_TRANSITION}>
        {#if node.children}
          {#each node.children as child (child.id)}
            {@render groupNode(child, level + 1)}
          {/each}
        {:else if node.rows && node.rows.length}
          {#each node.rows as item (item.key)}
            <div class="gl-row" style="--gl-row-level: {level}">
              {@render row(item)}
            </div>
          {/each}
        {:else}
          <div class="gl-empty">{node.emptyText ?? 'No tools.'}</div>
        {/if}
      </div>
    {/if}
  </div>
{/snippet}

<style>
  .gl {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  /* Top-level groups are readable cards (matches the global panel idiom so it
     reads cleanly across midnight/light/platinum). Nested groups are flush. */
  .gl-group.top {
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    overflow: hidden;
  }
  .gl-group:not(.top) {
    border-top: 1px solid var(--border-subtle);
  }

  .gl-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    /* Indent deepens with nesting level. */
    padding: var(--spacing-sm) var(--spacing-md);
    padding-left: calc(var(--spacing-md) + var(--gl-level) * 16px);
    background: transparent;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    text-align: left;
    font: inherit;
  }
  .gl-head:hover { background: var(--bg-hover); }

  /* Solid grey title bar for the top-level cards. */
  .gl-head.top {
    min-height: 40px;
    background: var(--bg-elevated-2);
  }
  .gl-head.top.open { border-bottom: 1px solid var(--border-default); }

  .gl-chevron {
    display: inline-flex;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }
  .gl-chevron.open { transform: rotate(90deg); }
  .gl-head :global(svg) { color: var(--text-muted); }

  .gl-name {
    flex: 1;
    min-width: 0;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .gl-head.top .gl-name { font-weight: 700; }
  .gl-group:not(.top) > .gl-head .gl-name { font-weight: 500; }
  .gl-group.dormant > .gl-head .gl-name { color: var(--text-muted); }

  .gl-count {
    flex-shrink: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    padding: 0 6px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated-2);
  }
  .gl-head.top .gl-count {
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
    color: var(--accent-primary);
  }

  /* Row wrapper owns layout + per-depth indentation; the row snippet renders
     the inner info block and trailing controls (styled by the host panel). */
  .gl-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    padding-left: calc(var(--spacing-md) + (var(--gl-row-level) + 1) * 16px);
    border-top: 1px solid var(--border-subtle);
  }
  .gl-body > .gl-row:first-child { border-top: none; }

  .gl-empty {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-xs);
    font-style: italic;
    color: var(--text-muted);
  }
</style>
