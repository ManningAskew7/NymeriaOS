<script lang="ts">
  import { slide } from 'svelte/transition';
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { filterToolSearch } from '$lib/utils/toolSearch';
  import { computeEffectiveToolCounts, isMcpToolName, liveTemporaryToolNames } from '$lib/utils/toolCounts';
  import { CATEGORY_ORDER, getCategoryInfo } from '$lib/utils/toolCategories';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import type { TemporaryToolEntry, ToolSearchResult } from '$lib/types';
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Per-thread Tools tab. Merges the old Tools + MCP tabs into a single
   * "Enabled / Available" view over one scroll surface (the parent modal body).
   * No core/optional framing: section 1 is what is active for THIS thread,
   * section 2 is everything else, grouped by category and by MCP server.
   */
  interface Props {
    threadId: string;
    disabledTools: Set<string>;
    enabledTools: Set<string>;
    temporaryTools?: Record<string, TemporaryToolEntry> | null;
    // Transient UI state is owned by the parent panel so it survives tab
    // switches (this component is unmounted/remounted as the active tab changes).
    query?: string;
    expanded?: Set<string>;
    collapsed?: Set<string>;
  }

  let {
    threadId,
    disabledTools = $bindable(),
    enabledTools = $bindable(),
    temporaryTools = null,
    query = $bindable(''),
    expanded = $bindable(new Set()),
    collapsed = $bindable(new Set()),
  }: Props = $props();

  type ToolItem = {
    name: string;        // full tool name (mcp__server__tool for MCP)
    shortName: string;   // display name
    description: string;
    category: string;    // category key, or 'mcp_server'
    isMcp: boolean;
    serverId?: string;
    serverName?: string;
    serverEnabled?: boolean;
    isDefault: boolean;  // part of the user's global core set
    isTemporary: boolean;
    expiresAt?: string | null;
  };

  // Map categories to icons that actually exist in the Icon set (CATEGORY_INFO
  // references several kebab-case Lucide names the local set doesn't define).
  const CATEGORY_ICON: Record<string, string> = {
    general: 'terminal', profile: 'user', notepad: 'pin', self_modify: 'edit',
    todo: 'check', subagent: 'refresh', trigger: 'bolt', email: 'send',
    browser: 'folder', image: 'image', calendar: 'calendar', google_docs: 'fileText',
    integrations: 'server', skills: 'bolt', custom: 'tool', mcp_server: 'server',
  };
  const categoryIcon = (c: string) => CATEGORY_ICON[c] ?? 'tool';

  const searchActive = $derived(query.trim().length > 0);

  const loadError = $derived(unifiedToolsStore.error || defaultToolsStore.error);
  const ready = $derived(defaultToolsStore.loaded && unifiedToolsStore.loaded);
  const loading = $derived(!ready);

  // MCP tools grouped by server (same derivation the old inline MCP tab used).
  const mcpServersForThread = $derived.by(() => {
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return mcpServersStore.servers.map((server) => ({
      id: server.id,
      name: server.name,
      enabled: server.enabled,
      tools: server.discoveredTools.map((t) => {
        const mcpName = `mcp__${server.id}__${t.name}`;
        return { mcpName, shortName: t.name, description: t.description, isDefault: coreSet.has(mcpName) };
      }),
    }));
  });

  const liveTempNames = $derived(new Set(liveTemporaryToolNames(temporaryTools)));

  // The authoritative active-for-this-thread name set (default ∪ enabled ∪
  // live-temporary, minus disabled) — identical math to the save path.
  const enabledNameSet = $derived(
    computeEffectiveToolCounts({
      defaultToolNames: defaultToolsStore.defaultToolNames,
      enabledTools: [...enabledTools],
      disabledTools: [...disabledTools],
      temporaryTools: [...liveTempNames],
    }).activeNames
  );

  // Universe of every tool we can show, native + MCP.
  const allItems = $derived.by(() => {
    const items: ToolItem[] = [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);

    const meta = new Map<string, { description: string; category: string }>();
    for (const t of defaultToolsStore.tools) meta.set(t.name, { description: t.description, category: t.category });
    for (const t of unifiedToolsStore.tools) {
      const existing = meta.get(t.name);
      if (!existing || !existing.description) meta.set(t.name, { description: t.description, category: t.category });
    }

    const names = new Set<string>();
    for (const t of defaultToolsStore.tools) if (!isMcpToolName(t.name) && t.category !== 'mcp_server') names.add(t.name);
    for (const t of unifiedToolsStore.tools) if (!isMcpToolName(t.name) && t.category !== 'mcp_server') names.add(t.name);
    // Stale per-thread overrides may name tools no longer in the catalog; keep
    // them visible so the user can still see/clear the override.
    for (const n of enabledTools) if (!isMcpToolName(n)) names.add(n);
    for (const n of disabledTools) if (!isMcpToolName(n)) names.add(n);
    for (const n of liveTempNames) if (!isMcpToolName(n)) names.add(n);

    for (const name of names) {
      const m = meta.get(name);
      items.push({
        name,
        shortName: name,
        description: m?.description ?? '',
        category: m?.category ?? 'general',
        isMcp: false,
        isDefault: coreSet.has(name),
        isTemporary: liveTempNames.has(name),
        expiresAt: temporaryTools?.[name]?.expiresAt ?? null,
      });
    }

    for (const server of mcpServersForThread) {
      for (const t of server.tools) {
        items.push({
          name: t.mcpName,
          shortName: t.shortName,
          description: t.description,
          category: 'mcp_server',
          isMcp: true,
          serverId: server.id,
          serverName: server.name,
          serverEnabled: server.enabled,
          isDefault: t.isDefault,
          isTemporary: liveTempNames.has(t.mcpName),
          expiresAt: temporaryTools?.[t.mcpName]?.expiresAt ?? null,
        });
      }
    }
    return items;
  });

  const searchFields = (item: ToolItem) => ({
    name: item.name,
    description: item.description,
    category: item.category,
    tags: item.isMcp ? ['mcp', item.serverName ?? ''] : [],
  });

  const filtered = $derived(filterToolSearch(allItems, query, searchFields));
  const enabledItems = $derived(filtered.filter((i) => enabledNameSet.has(i.name)));
  const availableItems = $derived(filtered.filter((i) => !enabledNameSet.has(i.name)));

  function buildGroups(items: ToolItem[]) {
    const byCat = new Map<string, ToolItem[]>();
    const byServer = new Map<string, { id: string; name: string; enabled: boolean; items: ToolItem[] }>();
    for (const it of items) {
      if (it.isMcp) {
        let g = byServer.get(it.serverId!);
        if (!g) {
          g = { id: it.serverId!, name: it.serverName ?? it.serverId!, enabled: it.serverEnabled ?? true, items: [] };
          byServer.set(it.serverId!, g);
        }
        g.items.push(it);
      } else {
        const arr = byCat.get(it.category) ?? [];
        arr.push(it);
        byCat.set(it.category, arr);
      }
    }
    const catGroups: { key: string; title: string; icon: string; items: ToolItem[] }[] = [];
    const seen = new Set<string>();
    for (const cat of CATEGORY_ORDER) {
      const arr = byCat.get(cat);
      if (arr?.length) { catGroups.push({ key: cat, title: getCategoryInfo(cat).name, icon: categoryIcon(cat), items: sortItems(arr) }); seen.add(cat); }
    }
    for (const [cat, arr] of byCat) {
      if (!seen.has(cat) && arr.length) catGroups.push({ key: cat, title: getCategoryInfo(cat).name, icon: categoryIcon(cat), items: sortItems(arr) });
    }
    const serverGroups = [...byServer.values()]
      .map((g) => ({ ...g, items: sortItems(g.items) }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return { catGroups, serverGroups };
  }

  function sortItems(items: ToolItem[]): ToolItem[] {
    return [...items].sort((a, b) => a.shortName.localeCompare(b.shortName));
  }

  const enabledGroups = $derived(buildGroups(enabledItems));
  const availableGroups = $derived(buildGroups(availableItems));

  // Installed MCP servers that discovered no tools still get a visible entry
  // (with a rediscover hint), matching the old MCP tab so a misconfigured or
  // not-yet-running server is not silently hidden.
  const emptyMcpServers = $derived(mcpServersForThread.filter((s) => s.tools.length === 0));

  // Group collapse state. Enabled groups default open, Available groups default
  // closed; an active search forces every matching group open.
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
    if (open) { c.add(id); e.delete(id); } else { e.add(id); c.delete(id); }
    expanded = e;
    collapsed = c;
  }

  // Toggle a tool on/off, routing through the same two sets the save path uses.
  // A tool can be active via the global default set, an explicit enabledTools
  // pin, or a live temporary (TTL) binding. "activeWithoutPin" = it would stay
  // on even without an enabledTools entry, so suppressing it needs a disabled
  // override rather than removing a (possibly non-existent) pin.
  function setToolEnabled(item: ToolItem, on: boolean) {
    const activeWithoutPin = item.isDefault || item.isTemporary;
    const d = new Set(disabledTools);
    const e = new Set(enabledTools);
    if (on) {
      // Clear any off-override; pin it on only if nothing else keeps it active.
      // An existing pin is left intact.
      d.delete(item.name);
      if (!activeWithoutPin) e.add(item.name);
    } else if (activeWithoutPin) {
      // Active via default/temporary: the only way to suppress it is an explicit
      // off-override. Do NOT drop an existing enabledTools pin, so it survives a
      // later re-enable or TTL expiry.
      d.add(item.name);
    } else {
      // Pure optional: removing the pin turns it off.
      e.delete(item.name);
    }
    disabledTools = d;
    enabledTools = e;
  }

  function toggleTool(item: ToolItem) {
    setToolEnabled(item, !enabledNameSet.has(item.name));
  }

  // Backend semantic-search fallback for names the local fuzzy ranker missed.
  let backendResults = $state<ToolSearchResult[]>([]);
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;
  let searchGen = 0;
  $effect(() => {
    const q = query.trim();
    if (searchDebounce) { clearTimeout(searchDebounce); searchDebounce = null; }
    if (q.length < 2) { backendResults = []; return; }
    const gen = ++searchGen;
    searchDebounce = setTimeout(async () => {
      try {
        const resp = await api.searchTools({ query: q, threadId, topK: 20, includeStatus: false });
        if (gen !== searchGen) return;
        backendResults = resp.results;
      } catch {
        if (gen === searchGen) backendResults = [];
      }
    }, 200);
    // Cancel the pending request if the query changes or the component unmounts
    // (the tab body is remounted on every tab switch).
    return () => {
      if (searchDebounce) {
        clearTimeout(searchDebounce);
        searchDebounce = null;
      }
    };
  });

  const backendExtras = $derived.by(() => {
    if (!searchActive || backendResults.length === 0) return [] as ToolItem[];
    const shown = new Set(filtered.map((i) => i.name));
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    const out: ToolItem[] = [];
    for (const r of backendResults) {
      if (r.name.startsWith('mcp__') || r.toolType === 'mcp_server') continue;
      if (shown.has(r.name)) continue;
      out.push({
        name: r.name,
        shortName: r.name,
        description: r.description,
        category: r.category ?? 'general',
        isMcp: false,
        isDefault: coreSet.has(r.name),
        isTemporary: false,
        expiresAt: null,
      });
    }
    return out;
  });
</script>

<div class="tools-tab">
  <div class="tools-search">
    <input
      type="text"
      class="search-field"
      bind:value={query}
      placeholder="Search tools by name or description..."
      aria-label="Search tools"
    />
    {#if query}
      <button class="search-clear" type="button" onclick={() => (query = '')} aria-label="Clear search">
        <Icon name="x" size={14} />
      </button>
    {/if}
  </div>

  {#if loadError}
    <div class="tools-msg">{loadError}</div>
  {:else if loading}
    <div class="tools-msg">Loading tools...</div>
  {:else}
    <!-- Enabled for this thread -->
    <ThreadSettingsSection
      title="Enabled for this thread"
      icon="check"
      count={enabledItems.length}
      description="Tools the agent can use in this thread."
      flush
    >
      {#if enabledItems.length === 0}
        <div class="tools-msg subtle">
          {searchActive ? 'No enabled tools match your search.' : 'No tools enabled for this thread.'}
        </div>
      {:else}
        {#each enabledGroups.catGroups as g (g.key)}
          {@render toolGroup(`enab:cat:${g.key}`, g.title, g.icon, g.items, false, true)}
        {/each}
        {#each enabledGroups.serverGroups as g (g.id)}
          {@render toolGroup(`enab:srv:${g.id}`, g.name, 'server', g.items, !g.enabled, true)}
        {/each}
      {/if}
    </ThreadSettingsSection>

    <!-- Available to add -->
    <ThreadSettingsSection
      title="Available to add"
      icon="plus"
      count={availableItems.length + backendExtras.length}
      description="Not enabled here. Toggle on to add for this thread only."
      flush
    >
      {#if availableItems.length === 0 && backendExtras.length === 0 && emptyMcpServers.length === 0}
        <div class="tools-msg subtle">
          {searchActive ? 'No other tools match your search.' : 'Every available tool is already enabled.'}
        </div>
      {:else}
        {#if backendExtras.length > 0}
          {@render toolGroup('avail:more', 'More from search', 'tool', backendExtras, false, true)}
        {/if}
        {#each availableGroups.catGroups as g (g.key)}
          {@render toolGroup(`avail:cat:${g.key}`, g.title, g.icon, g.items, false, false)}
        {/each}
        {#each availableGroups.serverGroups as g (g.id)}
          {@render toolGroup(`avail:srv:${g.id}`, g.name, 'server', g.items, !g.enabled, false)}
        {/each}
        {#if !searchActive}
          {#each emptyMcpServers as s (s.id)}
            {@render toolGroup(`avail:srv:${s.id}`, s.name, 'server', [], !s.enabled, false)}
          {/each}
        {/if}
      {/if}
    </ThreadSettingsSection>

    <p class="tools-foot-hint">
      Install or remove MCP servers and create custom tools in
      <strong>Settings → Tools / MCP</strong>. Changes there appear here automatically.
    </p>
  {/if}
</div>

<!-- Collapsible group, renders rows only when open. Used for both Enabled
     (defaultOpen) and Available (default closed) sections. -->
{#snippet toolGroup(id: string, title: string, icon: string, items: ToolItem[], dormant: boolean, defaultOpen: boolean)}
  {@const open = groupOpen(id, defaultOpen)}
  <div class="group" class:dormant>
    <button class="group-head" type="button" onclick={() => toggleGroup(id, defaultOpen)} aria-expanded={open}>
      <span class="group-chevron" class:open><Icon name="chevronRight" size={14} /></span>
      <Icon name={icon} size={13} />
      <span class="group-name">{title}</span>
      <span class="group-count">{items.length}</span>
    </button>
    {#if open}
      <div class="group-body" transition:slide={DROPDOWN_TRANSITION}>
        {#if items.length === 0}
          <div class="empty-server">No tools discovered. Rediscover in Settings → MCP.</div>
        {:else}
          {#each items as item (item.name)}
            {@render toolRow(item)}
          {/each}
        {/if}
      </div>
    {/if}
  </div>
{/snippet}

{#snippet toolRow(item: ToolItem)}
  {@const enabled = enabledNameSet.has(item.name)}
  {@const dormant = item.isMcp && item.serverEnabled === false}
  <div class="tool-row" class:dormant>
    <div class="tool-info">
      <span class="tool-name">
        {item.shortName}
        {#if item.isTemporary}<span class="tool-tag" title="Temporary tool bound with a TTL (e.g. by a Skill Kit)">temporary</span>{/if}
        {#if dormant}<span class="tool-tag muted" title="MCP server is stopped. Enable it in Settings → MCP.">stopped</span>{/if}
      </span>
      <span class="tool-desc">{item.description || 'No description'}</span>
    </div>
    <ToggleSwitch
      checked={enabled}
      onclick={() => toggleTool(item)}
      title={enabled ? 'Disable for this thread' : 'Enable for this thread'}
      ariaLabel={`${enabled ? 'Disable' : 'Enable'} ${item.shortName} for this thread`}
    />
  </div>
{/snippet}

<style>
  .tools-tab {
    padding: var(--spacing-lg);
  }

  .tools-search {
    position: relative;
    margin-bottom: var(--spacing-md);
  }

  .search-field {
    width: 100%;
    padding: var(--spacing-sm) 32px var(--spacing-sm) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }
  .search-field:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .search-field::placeholder { color: var(--text-muted); }

  .search-clear {
    position: absolute;
    top: 50%;
    right: 6px;
    transform: translateY(-50%);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
  }
  .search-clear:hover { color: var(--text-primary); background: var(--bg-hover); }

  .tools-msg {
    padding: var(--spacing-md);
    text-align: center;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }
  .tools-msg.subtle { font-size: var(--font-size-xs); }

  .tools-foot-hint {
    margin: var(--spacing-xs) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
  }

  .empty-server {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-xs);
    font-style: italic;
    color: var(--text-muted);
  }

  /* Collapsible group */
  .group { border-top: 1px solid var(--border-subtle); }
  .group:first-child { border-top: none; }
  .group-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: transparent;
    border: none;
    color: var(--text-secondary);
    cursor: pointer;
    text-align: left;
  }
  .group-head:hover { background: var(--bg-hover); }
  .group-chevron {
    display: inline-flex;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }
  .group-chevron.open { transform: rotate(90deg); }
  .group-name {
    flex: 1;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }
  .group-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    padding: 0 6px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated-2);
  }
  .group.dormant .group-name { color: var(--text-muted); }

  /* Tool row */
  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md) var(--spacing-sm) calc(var(--spacing-md) + 18px);
    border-top: 1px solid var(--border-subtle);
  }
  .tool-row:first-child { border-top: none; }
  .tool-row.dormant { opacity: 0.55; }

  .tool-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .tool-name {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tool-tag {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
  }
  .tool-tag.muted {
    color: var(--text-muted);
    background: var(--bg-elevated-2);
  }
</style>
