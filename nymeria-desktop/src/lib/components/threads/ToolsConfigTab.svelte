<script lang="ts">
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { filterToolSearch } from '$lib/utils/toolSearch';
  import { computeEffectiveToolCounts, isMcpToolName, liveTemporaryToolNames } from '$lib/utils/toolCounts';
  import { getCategoryInfo } from '$lib/utils/toolCategories';
  import type { GroupedToolItem, TemporaryToolEntry, ToolSearchResult } from '$lib/types';
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';
  import ToolGroupedList from '$lib/components/tools/ToolGroupedList.svelte';

  /**
   * Per-thread Tools pane for a single source (native or mcp), chosen by the
   * `section` prop from the settings sidebar. Shows an "Enabled for this thread"
   * / "Available to add" split over the one scroll surface (the parent modal
   * body). No core/optional framing. Grouping, nesting, collapse and
   * relevance-ordered rendering are delegated to the shared ToolGroupedList.
   */
  interface Props {
    threadId: string;
    /** Which tool source this pane renders. */
    section?: 'native' | 'mcp';
    disabledTools: Set<string>;
    enabledTools: Set<string>;
    temporaryTools?: Record<string, TemporaryToolEntry> | null;
    // Transient UI state is owned by the parent panel so it survives tab
    // switches (this component is unmounted/remounted as the active tab changes).
    query?: string;
    expanded?: Set<string>;
    collapsed?: Set<string>;
    // Open/closed state of the two pools (Enabled / Available), hoisted like the
    // rest so it survives the remount. Mirrors the global panel's Core/Available.
    enabledOpen?: boolean;
    availableOpen?: boolean;
  }

  let {
    threadId,
    section = 'native',
    disabledTools = $bindable(),
    enabledTools = $bindable(),
    temporaryTools = null,
    query = $bindable(''),
    expanded = $bindable(new Set()),
    collapsed = $bindable(new Set()),
    enabledOpen = $bindable(true),
    availableOpen = $bindable(false),
  }: Props = $props();

  type ToolItem = {
    name: string;
    shortName: string;
    description: string;
    category: string;
    group?: string | null;
    groupLabel?: string | null;
    service?: string | null;
    serviceLabel?: string | null;
    isMcp: boolean;
    serverId?: string;
    serverName?: string;
    serverEnabled?: boolean;
    isDefault: boolean;
    isTemporary: boolean;
    expiresAt?: string | null;
  };

  const searchActive = $derived(query.trim().length > 0);

  const loadError = $derived(unifiedToolsStore.error || defaultToolsStore.error);
  const ready = $derived(defaultToolsStore.loaded && unifiedToolsStore.loaded);
  const loading = $derived(!ready);

  // A failed catalog fetch leaves the stores `loaded` (so the panel effect does
  // not loop), which means search would otherwise run over a shrunken universe
  // with no recovery. Let the error state re-fetch both stores on demand.
  let retrying = $state(false);
  async function retryLoad() {
    retrying = true;
    try {
      mcpServersStore.refresh();
      await Promise.all([unifiedToolsStore.reload(), defaultToolsStore.reload()]);
    } finally {
      retrying = false;
    }
  }

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

  const enabledNameSet = $derived(
    computeEffectiveToolCounts({
      defaultToolNames: defaultToolsStore.defaultToolNames,
      enabledTools: [...enabledTools],
      disabledTools: [...disabledTools],
      temporaryTools: [...liveTempNames],
    }).activeNames
  );

  type Meta = {
    description: string;
    category: string;
    group?: string | null;
    groupLabel?: string | null;
    service?: string | null;
    serviceLabel?: string | null;
  };

  const allItems = $derived.by(() => {
    const items: ToolItem[] = [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);

    const meta = new Map<string, Meta>();
    for (const t of defaultToolsStore.tools) {
      meta.set(t.name, {
        description: t.description, category: t.category,
        group: t.group, groupLabel: t.group_label, service: t.service, serviceLabel: t.service_label,
      });
    }
    for (const t of unifiedToolsStore.tools) {
      const existing = meta.get(t.name);
      if (!existing || !existing.description) {
        meta.set(t.name, {
          description: t.description, category: t.category,
          group: t.group, groupLabel: t.groupLabel, service: t.service, serviceLabel: t.serviceLabel,
        });
      }
    }

    const names = new Set<string>();
    for (const t of defaultToolsStore.tools) if (!isMcpToolName(t.name) && t.category !== 'mcp_server') names.add(t.name);
    for (const t of unifiedToolsStore.tools) if (!isMcpToolName(t.name) && t.category !== 'mcp_server') names.add(t.name);
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
        group: m?.group ?? null,
        groupLabel: m?.groupLabel ?? null,
        service: m?.service ?? null,
        serviceLabel: m?.serviceLabel ?? null,
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
    shortName: item.shortName,
    description: item.description,
    // Search the human-readable category, functional group and service labels
    // (e.g. "Google Docs", "CRM, Sales & Lead Enrichment", "Salesforce"), and
    // keep the raw keys + MCP server name as tags, so all vocabularies match.
    category: getCategoryInfo(item.category).name,
    tags: item.isMcp
      ? ['mcp', item.serverName ?? '', item.category]
      : [item.category, item.groupLabel ?? '', item.serviceLabel ?? ''],
  });

  const filtered = $derived(filterToolSearch(allItems, query, searchFields));
  const enabledItems = $derived(filtered.filter((i) => enabledNameSet.has(i.name)));
  const availableItems = $derived(filtered.filter((i) => !enabledNameSet.has(i.name)));

  // Backend semantic-search fallback for names the local fuzzy ranker missed.
  let backendResults = $state<ToolSearchResult[]>([]);
  let searching = $state(false);
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;
  let searchGen = 0;
  $effect(() => {
    const q = query.trim();
    if (searchDebounce) { clearTimeout(searchDebounce); searchDebounce = null; }
    // Bump the generation up front so an in-flight fetch from a previous query
    // is invalidated even on the clear / short-query path (no stale flash-back).
    const gen = ++searchGen;
    if (q.length < 2) { backendResults = []; searching = false; return; }
    searching = true;
    searchDebounce = setTimeout(async () => {
      try {
        const resp = await api.searchTools({ query: q, threadId, topK: 30, includeStatus: false });
        if (gen !== searchGen) return;
        backendResults = resp.results;
      } catch {
        if (gen === searchGen) backendResults = [];
      } finally {
        if (gen === searchGen) searching = false;
      }
    }, 200);
    return () => {
      if (searchDebounce) {
        clearTimeout(searchDebounce);
        searchDebounce = null;
      }
    };
  });

  // Extras from backend semantic search for names the local ranker missed,
  // split by source. They carry the backend grouping fields, so they fold into
  // the right functional-group -> service sub-group rather than a flat bucket.
  // Already-shown and already-enabled tools are excluded.
  const backendExtras = $derived.by(() => {
    const out = { native: [] as ToolItem[], mcp: [] as ToolItem[] };
    if (!searchActive || backendResults.length === 0) return out;
    const shown = new Set(filtered.map((i) => i.name));
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    const serverNameById = new Map(mcpServersForThread.map((s) => [s.id, s.name]));
    const discoveredMcpNames = new Set(mcpServersForThread.flatMap((s) => s.tools.map((t) => t.mcpName)));
    for (const r of backendResults) {
      if (shown.has(r.name) || enabledNameSet.has(r.name)) continue;
      if (r.name.startsWith('mcp__') || r.toolType === 'mcp_server') {
        const parts = r.name.split('__');
        const serverId = parts[1] ?? '';
        // Only surface MCP tools this thread's servers have actually discovered:
        // the backend indexes the global MCP catalog, but a name absent from
        // allItems would make the row vanish the moment it is toggled on.
        if (!discoveredMcpNames.has(r.name)) continue;
        out.mcp.push({
          name: r.name,
          shortName: parts.slice(2).join('__') || r.name,
          description: r.description,
          category: 'mcp_server',
          isMcp: true,
          serverId,
          serverName: serverNameById.get(serverId) ?? serverId,
          serverEnabled: true,
          isDefault: coreSet.has(r.name),
          isTemporary: false,
          expiresAt: null,
        });
      } else {
        out.native.push({
          name: r.name,
          shortName: r.name,
          description: r.description,
          category: r.category ?? 'general',
          group: r.group ?? null,
          groupLabel: r.groupLabel ?? null,
          service: r.service ?? null,
          serviceLabel: r.serviceLabel ?? null,
          isMcp: false,
          isDefault: coreSet.has(r.name),
          isTemporary: false,
          expiresAt: null,
        });
      }
    }
    return out;
  });

  function toGrouped(item: ToolItem): GroupedToolItem<ToolItem> {
    return {
      key: item.name,
      label: item.shortName,
      category: item.category,
      group: item.group ?? null,
      groupLabel: item.groupLabel ?? null,
      service: item.service ?? null,
      serviceLabel: item.serviceLabel ?? null,
      serverId: item.serverId,
      serverName: item.serverName,
      serverEnabled: item.serverEnabled,
      data: item,
    };
  }

  // Normalized pools for the shared list. Available pools fold in the backend
  // search extras so they group naturally; the relevance order is preserved.
  const nativeEnabled = $derived(enabledItems.filter((i) => !i.isMcp).map(toGrouped));
  const nativeAvail = $derived([...availableItems.filter((i) => !i.isMcp), ...backendExtras.native].map(toGrouped));
  const mcpEnabled = $derived(enabledItems.filter((i) => i.isMcp).map(toGrouped));
  const mcpAvail = $derived([...availableItems.filter((i) => i.isMcp), ...backendExtras.mcp].map(toGrouped));

  // Installed MCP servers that discovered no tools still get a visible entry.
  const emptyMcpServers = $derived(
    mcpServersForThread.filter((s) => s.tools.length === 0).map((s) => ({ id: s.id, name: s.name, enabled: s.enabled }))
  );

  // Toggle a tool on/off, routing through the same two sets the save path uses.
  // "activeWithoutPin" tools (default or live-temporary) stay on without an
  // enabledTools entry, so suppressing them needs a disabled override.
  function setToolEnabled(item: ToolItem, on: boolean) {
    const activeWithoutPin = item.isDefault || item.isTemporary;
    const d = new Set(disabledTools);
    const e = new Set(enabledTools);
    if (on) {
      d.delete(item.name);
      if (!activeWithoutPin) e.add(item.name);
    } else if (activeWithoutPin) {
      // Don't drop an existing pin, so it survives a re-enable or TTL expiry.
      d.add(item.name);
    } else {
      e.delete(item.name);
    }
    disabledTools = d;
    enabledTools = e;
  }

  function toggleTool(item: ToolItem) {
    setToolEnabled(item, !enabledNameSet.has(item.name));
  }
</script>

<div class="tools-tab">
  {#if loadError}
    <div class="tools-msg">
      <p>{loadError}</p>
      <button class="retry-btn" type="button" onclick={retryLoad} disabled={retrying}>
        {retrying ? 'Retrying...' : 'Retry'}
      </button>
    </div>
  {:else if loading}
    <div class="tools-msg">Loading tools...</div>
  {:else}
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
    {#if searching}
      <div class="search-status">Searching the full catalog...</div>
    {/if}

    {#if section === 'native'}
      <ThreadSettingsSection title="Enabled for this thread" count={nativeEnabled.length} description="Native tools the agent can use in this thread." flush collapsible bind:open={enabledOpen}>
        {#if nativeEnabled.length === 0}
          <div class="tools-msg subtle">
            {searchActive ? 'No enabled native tools match your search.' : 'No native tools enabled for this thread.'}
          </div>
        {:else}
          <ToolGroupedList items={nativeEnabled} {searchActive} poolKey="nat-enab" defaultOpenTopLevel bind:expanded bind:collapsed row={toolRow} />
        {/if}
      </ThreadSettingsSection>

      <ThreadSettingsSection title="Available to add" count={nativeAvail.length} description="Not enabled here. Toggle on to add for this thread only." flush collapsible bind:open={availableOpen}>
        {#if nativeAvail.length === 0}
          <div class="tools-msg subtle">
            {searchActive ? 'No other native tools match your search.' : 'Every native tool is already enabled.'}
          </div>
        {:else}
          <ToolGroupedList items={nativeAvail} {searchActive} poolKey="nat-avail" bind:expanded bind:collapsed row={toolRow} />
        {/if}
      </ThreadSettingsSection>
    {:else}
      <ThreadSettingsSection title="Enabled for this thread" count={mcpEnabled.length} description="MCP server tools enabled in this thread." flush collapsible bind:open={enabledOpen}>
        {#if mcpEnabled.length === 0}
          <div class="tools-msg subtle">
            {searchActive ? 'No enabled MCP tools match your search.' : 'No MCP tools enabled for this thread.'}
          </div>
        {:else}
          <ToolGroupedList items={mcpEnabled} {searchActive} mode="server" poolKey="mcp-enab" defaultOpenTopLevel bind:expanded bind:collapsed row={toolRow} />
        {/if}
      </ThreadSettingsSection>

      <ThreadSettingsSection title="Available to add" count={mcpAvail.length + (searchActive ? 0 : emptyMcpServers.length)} description="MCP tools not enabled here. Add or remove servers in Settings -> MCP." flush collapsible bind:open={availableOpen}>
        {#if mcpAvail.length === 0 && (searchActive || emptyMcpServers.length === 0)}
          <div class="tools-msg subtle">
            {#if searchActive}
              No other MCP tools match your search.
            {:else if mcpServersForThread.length === 0}
              No MCP servers installed.
            {:else}
              Every MCP tool is already enabled.
            {/if}
          </div>
        {:else}
          <ToolGroupedList items={mcpAvail} {searchActive} mode="server" poolKey="mcp-avail" emptyServers={emptyMcpServers} bind:expanded bind:collapsed row={toolRow} />
        {/if}
      </ThreadSettingsSection>
    {/if}

    <p class="tools-foot-hint">
      Install or remove MCP servers and create custom tools in
      <strong>Settings → Tools / MCP</strong>. Changes there appear here automatically.
    </p>
  {/if}
</div>

{#snippet toolRow(gi: GroupedToolItem)}
  {@const item = gi.data as ToolItem}
  {@const enabled = enabledNameSet.has(item.name)}
  {@const dormant = item.isMcp && item.serverEnabled === false}
  <div class="tt-row" class:dormant>
    <div class="tt-info">
      <span class="tt-name">
        {item.shortName}
        {#if item.isTemporary}<span class="tt-tag" title="Temporary tool bound with a TTL (e.g. by a Skill Kit)">temporary</span>{/if}
        {#if dormant}<span class="tt-tag" title="MCP server is stopped. Enable it in Settings → MCP.">stopped</span>{/if}
      </span>
      <span class="tt-desc">{item.description || 'No description'}</span>
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

  .retry-btn {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .retry-btn:hover:not(:disabled) { background: var(--bg-hover); }
  .retry-btn:disabled { opacity: 0.6; cursor: default; }

  .search-status {
    margin: calc(-1 * var(--spacing-xs)) 0 var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .tools-foot-hint {
    margin: var(--spacing-md) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
  }

  /* Tool row inner content (the shared list owns the row wrapper + indent). */
  .tt-row {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }
  .tt-row.dormant { opacity: 0.55; }

  .tt-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .tt-name {
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
  .tt-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tt-tag {
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
  }
</style>
