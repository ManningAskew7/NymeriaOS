<script lang="ts">
  import { onMount } from 'svelte';
  import { toolsStore } from '$lib/stores/tools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { trapFocus } from '$lib/actions/focus';
  import type { CustomTool, CustomToolCreateRequest, UnifiedTool, DefaultToolInfo, ToolSearchResult, GroupedToolItem } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import InlineLoader from '../common/InlineLoader.svelte';
  import ToggleSwitch from '../common/ToggleSwitch.svelte';
  import ToolForm from './ToolForm.svelte';
  import ToolTestPanel from './ToolTestPanel.svelte';
  import ToolCountWarning from './ToolCountWarning.svelte';
  import ToolGroupedList from './ToolGroupedList.svelte';
  import { getCategoryInfo } from '$lib/utils/toolCategories';
  import { filterToolSearch } from '$lib/utils/toolSearch';

  // --- Default tools state (absorbed from DefaultToolsPanel) ---
  let selectedTools = $state<Set<string>>(new Set());
  let initialized = $state(false);
  let searchQuery = $state('');
  let backendSearchResults = $state<ToolSearchResult[]>([]);
  let searchDebounceHandle: ReturnType<typeof setTimeout> | null = null;
  let searchGeneration = 0;
  let showWarning = $state(false);
  let saveMessage = $state('');
  let saveStatus = $state<'idle' | 'success' | 'error'>('idle');

  // Tools whose runtime is gated by require_admin_user on the backend
  // (Nymeria/nymeria/triggers/api.py around the optional-tool toggle path).
  // For non-admin callers the toggle still works in the UI but the agent
  // will get 403 trying to invoke them — surface that up-front.
  const ADMIN_ONLY_TOOLS = new Set(['self_modify', 'claude_code']);
  let isAdmin = $derived(configStore.identity?.role === 'admin');
  function isAdminOnlyTool(name: string): boolean {
    return ADMIN_ONLY_TOOLS.has(name);
  }

  // --- Custom tools state ---
  let showCreateForm = $state(false);
  let editingTool = $state<CustomTool | null>(null);
  let testingTool = $state<CustomTool | null>(null);
  let customFilter = $state<'all' | 'http' | 'mcp'>('all');

  // Combined built-in tool editor (description override + dynamic config)
  let editingBuiltinTool = $state<UnifiedTool | null>(null);
  let descriptionInput = $state('');
  let configInputs = $state<Record<string, unknown>>({});

  // Collapsible state for Default / Available sections. Honour the user's
  // collapse choice unconditionally — searching does not force-open either
  // section, so the user can collapse one and watch the other update as
  // they type. The section header still shows the live match-count badge
  // when collapsed, so it's clear results landed in there.
  let defaultOpen = $state(true);
  let availableOpen = $state(false);

  // Per-card collapse state for the shared grouped lists (Default + Available
  // pools share these; distinct poolKeys keep their ids from colliding).
  let glExpanded = $state<Set<string>>(new Set());
  let glCollapsed = $state<Set<string>>(new Set());

  const isSearching = $derived(searchQuery.trim().length > 0);
  const effectiveDefaultOpen = $derived(defaultOpen);
  const effectiveAvailableOpen = $derived(availableOpen);

  // Force-refresh default tools on every mount so newly-installed MCP
  // servers (whose tools get registered in metadata) show up in the picker
  // without a full app reload. Other stores only need a first-load fetch.
  onMount(() => {
    defaultToolsStore.resetLoaded();
    defaultToolsStore.load();
  });

  $effect(() => {
    if (!toolsStore.loaded && !toolsStore.loading) {
      toolsStore.loadTools();
    }
    if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) {
      unifiedToolsStore.loadTools();
    }
  });

  // Initialize selection from loaded data
  $effect(() => {
    if (defaultToolsStore.loaded && !initialized) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      initialized = true;
    }
  });

  // Filtered tools by search
  function isMcpDefaultTool(tool: DefaultToolInfo): boolean {
    return tool.category === 'mcp_server' || tool.name.startsWith('mcp__');
  }

  const filteredTools = $derived.by(() => {
    const visibleTools = defaultToolsStore.tools.filter((tool) => !isMcpDefaultTool(tool));
    return filterToolSearch(visibleTools, searchQuery, (tool: DefaultToolInfo) => ({
      name: tool.name,
      description: tool.description,
      category: tool.category,
      toolType: 'builtin',
      tags: [tool.security_level]
    }));
  });

  const visibleDefaultToolCount = $derived(defaultToolsStore.tools.filter((tool) => !isMcpDefaultTool(tool)).length);
  const defaultCount = $derived(
    defaultToolsStore.tools.filter((tool) => !isMcpDefaultTool(tool) && selectedTools.has(tool.name)).length
  );
  const availableCount = $derived(visibleDefaultToolCount - defaultCount);
  const totalWithCallable = $derived(defaultCount + defaultToolsStore.callableThreadCount);

  // Normalize a built-in tool row for the shared grouped list. `data` carries
  // the original DefaultToolInfo back to the row snippet for badges/edit/toggle.
  function toGrouped(t: DefaultToolInfo): GroupedToolItem<DefaultToolInfo> {
    return {
      key: t.name,
      label: t.name,
      category: t.category,
      group: t.group ?? null,
      groupLabel: t.group_label ?? null,
      service: t.service ?? null,
      serviceLabel: t.service_label ?? null,
      data: t,
    };
  }

  // Backend semantic-search fallback. Surfaces tools the local ranker missed
  // (e.g. because the frontend pool was stale or the query fell under the
  // Dice-bigram cutoff). MCP-prefixed results are stripped; native search
  // only.
  $effect(() => {
    const q = searchQuery.trim();
    if (searchDebounceHandle) {
      clearTimeout(searchDebounceHandle);
      searchDebounceHandle = null;
    }
    if (q.length < 2) {
      backendSearchResults = [];
      return;
    }
    const myGen = ++searchGeneration;
    searchDebounceHandle = setTimeout(async () => {
      try {
        const resp = await api.searchTools({
          query: q,
          topK: 20,
          includeStatus: false,
        });
        if (myGen !== searchGeneration) return;
        backendSearchResults = resp.results.filter(
          (r) => r.toolType !== 'mcp_server' && !r.name.startsWith('mcp__')
        );
      } catch {
        if (myGen === searchGeneration) backendSearchResults = [];
      }
    }, 200);
  });

  function synthesizeDefaultToolInfo(r: ToolSearchResult, optional: boolean): DefaultToolInfo {
    return {
      name: r.name,
      description: r.description,
      category: r.category,
      security_level: r.securityLevel,
      is_optional: optional,
      is_default: r.isDefault,
      group: r.group ?? null,
      group_label: r.groupLabel ?? null,
      service: r.service ?? null,
      service_label: r.serviceLabel ?? null,
      auth_status: r.authStatus ?? null,
      auth_provider: r.authProvider ?? null,
    };
  }

  // Backend results that belong in the Default section (currently selected
  // tools) but weren't surfaced locally.
  const backendExtrasDefaultList = $derived.by(() => {
    if (backendSearchResults.length === 0) return [] as DefaultToolInfo[];
    const localNames = new Set(
      filteredTools.filter((t) => selectedTools.has(t.name)).map((t) => t.name)
    );
    const out: DefaultToolInfo[] = [];
    for (const r of backendSearchResults) {
      if (!selectedTools.has(r.name)) continue;
      if (localNames.has(r.name)) continue;
      const match = defaultToolsStore.tools.find((t) => t.name === r.name);
      out.push(match ?? synthesizeDefaultToolInfo(r, false));
    }
    return out;
  });

  // Backend results that belong in the Available section.
  const backendExtrasAvailableList = $derived.by(() => {
    if (backendSearchResults.length === 0) return [] as DefaultToolInfo[];
    const localNames = new Set(
      filteredTools.filter((t) => !selectedTools.has(t.name)).map((t) => t.name)
    );
    const out: DefaultToolInfo[] = [];
    for (const r of backendSearchResults) {
      if (selectedTools.has(r.name)) continue;
      if (localNames.has(r.name)) continue;
      const match = defaultToolsStore.tools.find((t) => t.name === r.name);
      out.push(match ?? synthesizeDefaultToolInfo(r, true));
    }
    return out;
  });

  // Normalized pools for the shared grouped list. Available folds in the
  // backend search extras so they group naturally; relevance order is kept.
  const defaultItems = $derived(
    [...filteredTools.filter((t) => selectedTools.has(t.name)), ...backendExtrasDefaultList].map(toGrouped)
  );
  const availItems = $derived(
    [...filteredTools.filter((t) => !selectedTools.has(t.name)), ...backendExtrasAvailableList].map(toGrouped)
  );
  // Section-header match badges (filtered + backend extras).
  const defaultMatchCount = $derived(defaultItems.length);
  const availableMatchCount = $derived(availItems.length);

  const hasChanges = $derived.by(() => {
    const saved = new Set(defaultToolsStore.defaultToolNames);
    if (selectedTools.size !== saved.size) return true;
    for (const t of selectedTools) {
      if (!saved.has(t)) return true;
    }
    return false;
  });

  function toggleTool(name: string) {
    const next = new Set(selectedTools);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
    }
    selectedTools = next;
  }

  function sectionCountLabel(matches: number, total: number): string {
    return isSearching ? `${matches} matches of ${total}` : `${total}`;
  }

  function handleSave() {
    if (totalWithCallable > 25) {
      showWarning = true;
    } else {
      doSave();
    }
  }

  async function doSave() {
    showWarning = false;
    const ok = await defaultToolsStore.save([...selectedTools]);
    if (ok) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      unifiedToolsStore.resetLoaded();
      await unifiedToolsStore.loadTools();
      saveStatus = 'success';
      saveMessage = 'Default tools saved!';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || "Couldn't save your default tools. Try again in a moment.";
    }
    setTimeout(() => { saveMessage = ''; saveStatus = 'idle'; }, 3000);
  }

  async function handleReset() {
    const mcpToolsToPreserve = defaultToolsStore.defaultToolNames.filter((name) => name.startsWith('mcp__'));
    const ok = await defaultToolsStore.reset();
    if (ok) {
      const mergedDefaults = Array.from(new Set([
        ...defaultToolsStore.defaultToolNames.filter((name) => !name.startsWith('mcp__')),
        ...mcpToolsToPreserve,
      ]));
      const savedAfterReset = new Set(defaultToolsStore.defaultToolNames);
      const needsMcpRestore =
        mergedDefaults.length !== savedAfterReset.size ||
        mergedDefaults.some((name) => !savedAfterReset.has(name));
      if (needsMcpRestore) {
        await defaultToolsStore.save(mergedDefaults);
      }
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      unifiedToolsStore.resetLoaded();
      await unifiedToolsStore.loadTools();
      saveStatus = 'success';
      saveMessage = 'Reset to NymeriaOS defaults';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || "Couldn't reset your default tools. Try again in a moment.";
    }
    setTimeout(() => { saveMessage = ''; saveStatus = 'idle'; }, 3000);
  }

  // --- Custom tools handlers ---
  const filteredCustomTools = $derived.by(() => {
    let result = toolsStore.tools;
    if (customFilter !== 'all') {
      result = result.filter((t) => t.implementationType === customFilter);
    }
    return filterToolSearch(result, searchQuery, (tool) => ({
      id: tool.id,
      name: tool.name,
      description: tool.description,
      category: 'custom',
      implementationType: tool.implementationType,
      tags: tool.tags
    }));
  });

  async function handleCreate(request: CustomToolCreateRequest) {
    const tool = await toolsStore.createTool(request);
    if (tool) {
      showCreateForm = false;
    }
  }

  async function handleUpdate(toolId: string, request: Partial<CustomToolCreateRequest>) {
    const tool = await toolsStore.updateTool(toolId, request);
    if (tool) {
      editingTool = null;
    }
  }

  async function handleDelete(tool: CustomTool) {
    if (confirm(`Are you sure you want to delete "${tool.name}"?`)) {
      await toolsStore.deleteTool(tool.id);
    }
  }

  async function handleToggleEnabled(tool: CustomTool) {
    await toolsStore.toggleToolEnabled(tool.id);
  }

  function formatDate(date: Date): string {
    return new Intl.DateTimeFormat('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    }).format(date);
  }

  // Open combined editor for a built-in tool by name (DefaultToolInfo rows only carry the name)
  function openBuiltinEditor(toolName: string) {
    const unified = unifiedToolsStore.tools.find(
      (t) => t.name === toolName && t.toolType === 'builtin'
    );
    if (!unified) return;
    editingBuiltinTool = unified;
    descriptionInput = unified.customDescription || unified.defaultDescription;
    configInputs = { ...unified.userConfig };
  }

  function getConfigFields(tool: UnifiedTool | null): Record<string, Record<string, unknown>> {
    const schema = tool?.configSchema as Record<string, unknown> | undefined;
    if (!schema) return {};

    const properties = schema.properties;
    if (properties && typeof properties === 'object' && !Array.isArray(properties)) {
      return properties as Record<string, Record<string, unknown>>;
    }

    return schema as Record<string, Record<string, unknown>>;
  }

  function isConfigRequired(tool: UnifiedTool, key: string, field: Record<string, unknown>): boolean {
    const required = (tool.configSchema as Record<string, unknown> | undefined)?.required;
    if (Array.isArray(required)) return required.includes(key);
    return Boolean(field.required);
  }

  function configValue(key: string, field: Record<string, unknown>, fallback: unknown = ''): unknown {
    return configInputs[key] ?? field.default ?? fallback;
  }

  function resetDescription() {
    if (editingBuiltinTool) {
      descriptionInput = editingBuiltinTool.defaultDescription;
    }
  }

  function updateConfigValue(key: string, value: unknown) {
    configInputs = { ...configInputs, [key]: value };
  }

  async function saveBuiltinTool() {
    if (!editingBuiltinTool) return;
    const tool = editingBuiltinTool;

    const trimmed = descriptionInput.trim();
    const nextDescription = trimmed === tool.defaultDescription ? null : trimmed;
    const descriptionChanged = nextDescription !== (tool.customDescription ?? null);

    const hasConfigSchema = Object.keys(getConfigFields(tool)).length > 0;
    const configChanged = hasConfigSchema &&
      JSON.stringify(configInputs) !== JSON.stringify(tool.userConfig);

    if (descriptionChanged) {
      await unifiedToolsStore.setToolDescription(tool.id, nextDescription);
    }
    if (configChanged) {
      await unifiedToolsStore.setToolConfig(tool.id, configInputs);
    }

    editingBuiltinTool = null;
    descriptionInput = '';
    configInputs = {};
  }

  function handleModalKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      if (showCreateForm) { showCreateForm = false; }
      else if (editingTool) { editingTool = null; }
      else if (testingTool) { testingTool = null; }
      else if (editingBuiltinTool) { editingBuiltinTool = null; }
    }
  }
</script>

<svelte:window onkeydown={handleModalKeydown} />

<div class="tool-management">
  <div class="panel-scroll">
  {#if defaultToolsStore.loading}
    <div class="loading"><InlineLoader text="Loading tools…" /></div>
  {:else if !defaultToolsStore.loaded}
    <div class="loading">Not connected to the backend. Open Settings → Backend to connect, then manage tools here.</div>
  {:else}
    <!-- Summary bar -->
    <div class="summary-bar">
      <div class="summary-left">
        <span class="summary-count">
          <strong>{defaultCount}</strong> default tools for new threads
        </span>
        {#if defaultToolsStore.callableThreadCount > 0}
          <span class="summary-callable">
            + {defaultToolsStore.callableThreadCount} callable threads
          </span>
        {/if}
        <span class="mode-badge custom">Custom</span>
      </div>
    </div>

    {#if totalWithCallable > 25}
      <div class="inline-warning">
        {totalWithCallable} tools total (including callable threads). High tool counts can degrade model performance
      </div>
    {/if}

    <!-- Search -->
    <div class="search-bar">
      <input
        type="text"
        class="search-input"
        bind:value={searchQuery}
        placeholder="Search tools…"
        aria-label="Search tools"
      />
    </div>

    <!-- Default Tools Section -->
    {#if defaultCount > 0}
      <div class="section-group" class:collapsed={!effectiveDefaultOpen}>
        <button
          class="section-header section-toggle"
          onclick={() => (defaultOpen = !defaultOpen)}
          type="button"
          aria-expanded={effectiveDefaultOpen}
        >
          <span class="section-chevron" class:open={effectiveDefaultOpen}>
            <Icon name="chevronRight" size={14} />
          </span>
          <span class="section-heading">Default tools</span>
          <span class="section-count">{sectionCountLabel(defaultMatchCount, defaultCount)}</span>
        </button>
        {#if effectiveDefaultOpen}
        <p class="section-hint">Loaded automatically in every new thread. Toggle off to move to Available.</p>
        {#if defaultItems.length === 0}
          <div class="section-empty">{isSearching ? `No default tools match "${searchQuery}".` : 'No default tools yet.'}</div>
        {:else}
          <div class="tools-body">
            <ToolGroupedList
              items={defaultItems}
              searchActive={isSearching}
              poolKey="default"
              defaultOpenTopLevel
              bind:expanded={glExpanded}
              bind:collapsed={glCollapsed}
              row={builtinRow}
            />
          </div>
        {/if}
        {/if}
      </div>
    {/if}

    <!-- Available Tools Section -->
    {#if availableCount > 0}
      <div class="section-group available-section" class:collapsed={!effectiveAvailableOpen}>
        <button
          class="section-header section-toggle"
          onclick={() => (availableOpen = !availableOpen)}
          type="button"
          aria-expanded={effectiveAvailableOpen}
        >
          <span class="section-chevron" class:open={effectiveAvailableOpen}>
            <Icon name="chevronRight" size={14} />
          </span>
          <span class="section-heading">Available Tools</span>
          <span class="section-count">{sectionCountLabel(availableMatchCount, availableCount)}</span>
        </button>
        {#if effectiveAvailableOpen}
        <p class="section-hint">Not loaded by default. Toggle on to add to your defaults, or enable per-thread in thread settings.</p>
        {#if availItems.length === 0}
          <div class="section-empty">{isSearching ? `No available tools match "${searchQuery}".` : 'No available tools.'}</div>
        {:else}
          <div class="tools-body">
            <ToolGroupedList
              items={availItems}
              searchActive={isSearching}
              poolKey="avail"
              bind:expanded={glExpanded}
              bind:collapsed={glCollapsed}
              row={builtinRow}
            />
          </div>
        {/if}
        {/if}
      </div>
    {/if}

  {/if}

  <!-- Custom Tools Section -->
  <div class="custom-tools-divider">
    <span>Custom Tools</span>
  </div>

  <div class="custom-tools-section">
    <div class="custom-tools-header">
      <p class="header-description">
        User-created HTTP and MCP tools that extend the agent's capabilities.
      </p>
      <Button variant="primary" onclick={() => (showCreateForm = true)}>
        + New Tool
      </Button>
    </div>

    {#if toolsStore.error}
      <div class="error-message">
        <Icon name="error" size={16} />
        {toolsStore.error}
        <button type="button" onclick={() => toolsStore.clearError()}>Dismiss</button>
      </div>
    {/if}

    {#if toolsStore.loading}
      <div class="loading"><InlineLoader text="Loading custom tools…" /></div>
    {:else if filteredCustomTools.length === 0}
      <div class="empty-state">
        <div class="empty-icon">+</div>
        <h3>{searchQuery.trim() ? 'No Custom Tools Match' : 'No Custom Tools Yet'}</h3>
        <p class="empty-message">
          {searchQuery.trim()
            ? 'Try another search or clear the field to view all custom tools.'
            : "You haven't created any custom tools yet. Add an HTTP tool to call a REST endpoint, or register an MCP server to expose its tools to Nymeria."}
        </p>
      </div>
    {:else}
      <div class="tool-list">
        {#each filteredCustomTools as tool (tool.id)}
          <div class="tool-item" class:disabled={!tool.enabled}>
            <div class="tool-info">
              <div class="tool-header">
                <span class="tool-name">{tool.name}</span>
                <span class="tool-type" class:http={tool.implementationType === 'http'}>
                  {tool.implementationType.toUpperCase()}
                </span>
              </div>
              <p class="tool-description">{tool.description}</p>
              <div class="tool-meta">
                <span class="tool-id">ID: {tool.id}</span>
                <span class="tool-date">Updated {formatDate(tool.updatedAt)}</span>
              </div>
            </div>
            <div class="tool-actions">
              <button
                class="action-btn"
                data-tooltip="Test tool"
                aria-label="Test tool"
                onclick={() => (testingTool = tool)}
              >
                <Icon name="play" size={16} />
              </button>
              <button
                class="action-btn"
                data-tooltip="Edit tool"
                aria-label="Edit tool"
                onclick={() => (editingTool = tool)}
              >
                <Icon name="edit" size={16} />
              </button>
              <button
                class="action-btn"
                class:enabled={tool.enabled}
                data-tooltip={tool.enabled ? 'Disable' : 'Enable'}
                aria-label={tool.enabled ? 'Disable tool' : 'Enable tool'}
                onclick={() => handleToggleEnabled(tool)}
              >
                <Icon name={tool.enabled ? 'visible' : 'hidden'} size={16} />
              </button>
              <button
                class="action-btn delete"
                data-tooltip="Delete tool"
                aria-label="Delete tool"
                onclick={() => handleDelete(tool)}
              >
                <Icon name="trash" size={16} />
              </button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
  </div>

  <!-- Pinned footer: Save/Reset for the default-tools selection -->
  {#if defaultToolsStore.loaded}
    <div class="panel-footer panel-footer-pinned">
      <Button variant="ghost" onclick={handleReset} disabled={defaultToolsStore.saving}>
        Reset to NymeriaOS Defaults
      </Button>
      <Button variant="primary" onclick={handleSave} disabled={defaultToolsStore.saving || !hasChanges}>
        {defaultToolsStore.saving ? 'Saving…' : 'Save Changes'}
      </Button>
    </div>

    {#if saveMessage}
      <div class="save-message" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
        {saveMessage}
      </div>
    {/if}
  {/if}

  <!-- Create form modal -->
  {#if showCreateForm}
    <div class="modal-overlay">
      <button
        class="modal-backdrop-button"
        type="button"
        tabindex="-1"
        aria-label="Close create custom tool dialog"
        onclick={() => (showCreateForm = false)}
      ></button>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="create-tool-title" tabindex="-1" use:trapFocus>
        <div class="modal-header">
          <h3 id="create-tool-title">Create Custom Tool</h3>
          <button class="close-btn" onclick={() => (showCreateForm = false)} type="button" aria-label="Close">
            &times;
          </button>
        </div>
        <ToolForm
          onSubmit={handleCreate}
          onCancel={() => (showCreateForm = false)}
        />
      </div>
    </div>
  {/if}

  <!-- Edit form modal -->
  {#if editingTool}
    <div class="modal-overlay">
      <button
        class="modal-backdrop-button"
        type="button"
        tabindex="-1"
        aria-label="Close edit custom tool dialog"
        onclick={() => (editingTool = null)}
      ></button>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="edit-tool-title" tabindex="-1" use:trapFocus>
        <div class="modal-header">
          <h3 id="edit-tool-title">Edit Tool: {editingTool.name}</h3>
          <button class="close-btn" onclick={() => (editingTool = null)} type="button" aria-label="Close">
            &times;
          </button>
        </div>
        <ToolForm
          tool={editingTool}
          onSubmit={(req) => handleUpdate(editingTool!.id, req)}
          onCancel={() => (editingTool = null)}
        />
      </div>
    </div>
  {/if}

  <!-- Test panel modal -->
  {#if testingTool}
    <div class="modal-overlay">
      <button
        class="modal-backdrop-button"
        type="button"
        tabindex="-1"
        aria-label="Close test tool dialog"
        onclick={() => (testingTool = null)}
      ></button>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="test-tool-title" tabindex="-1" use:trapFocus>
        <div class="modal-header">
          <h3 id="test-tool-title">Test Tool: {testingTool.name}</h3>
          <button class="close-btn" onclick={() => (testingTool = null)} type="button" aria-label="Close">
            &times;
          </button>
        </div>
        <ToolTestPanel
          tool={testingTool}
          onClose={() => (testingTool = null)}
        />
      </div>
    </div>
  {/if}

  <!-- Edit built-in tool modal (description override + dynamic configSchema) -->
  {#if editingBuiltinTool}
    {@const tool = editingBuiltinTool}
    {@const configFields = getConfigFields(tool)}
    {@const hasConfigSchema = Object.keys(configFields).length > 0}
    <div class="modal-overlay">
      <button
        class="modal-backdrop-button"
        type="button"
        tabindex="-1"
        aria-label="Close built-in tool settings dialog"
        onclick={() => (editingBuiltinTool = null)}
      ></button>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="edit-builtin-tool-title" tabindex="-1" use:trapFocus>
        <div class="modal-header">
          <h3 id="edit-builtin-tool-title">Edit Tool: {tool.name}</h3>
          <button class="close-btn" onclick={() => (editingBuiltinTool = null)} type="button" aria-label="Close">
            &times;
          </button>
        </div>
        <div class="modal-body">
          <div class="tool-meta-grid">
            <div class="meta-item">
              <span class="meta-label">Name</span>
              <code class="meta-value">{tool.name}</code>
            </div>
            <div class="meta-item">
              <span class="meta-label">Category</span>
              <span class="meta-value">{getCategoryInfo(String(tool.category)).name}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">Security</span>
              <span class="meta-value">{tool.securityLevel}</span>
            </div>
          </div>

          <div class="form-group">
            <span class="field-label">Original docstring</span>
            <p class="docstring-readonly">{tool.defaultDescription}</p>
          </div>

          <div class="form-group">
            <label for="description-input">Description shown to the agent</label>
            <p class="field-description">Override how this tool is described. The agent sees this text verbatim.</p>
            <textarea
              id="description-input"
              rows={4}
              bind:value={descriptionInput}
              placeholder="Enter a custom description…"
            ></textarea>
            {#if tool.customDescription}
              <div class="info-box">
                <Icon name="info" size={14} />
                <span>This tool has a custom description. Click "Reset to Default" to restore the original.</span>
              </div>
            {/if}
          </div>

          {#if hasConfigSchema}
            <div class="config-section">
              <span class="field-label">Configuration</span>
              <div class="config-form">
                {#each Object.entries(configFields) as [key, schema]}
                  {@const schemaObj = schema as Record<string, unknown>}
                  <div class="form-group">
                    <label for={`config-${key}`}>
                      {schemaObj.title || key}
                      {#if isConfigRequired(tool, key, schemaObj)}
                        <span class="required">*</span>
                      {/if}
                    </label>
                    {#if schemaObj.description}
                      <p class="field-description">{schemaObj.description}</p>
                    {/if}

                    {#if schemaObj.type === 'boolean'}
                      <label class="checkbox-label">
                        <input
                          type="checkbox"
                          id={`config-${key}`}
                          checked={Boolean(configValue(key, schemaObj, false))}
                          onchange={(e) => updateConfigValue(key, (e.target as HTMLInputElement).checked)}
                        />
                        <span>Enabled</span>
                      </label>
                    {:else if schemaObj.type === 'number' || schemaObj.type === 'integer'}
                      <input
                        type="number"
                        id={`config-${key}`}
                        value={configValue(key, schemaObj, 0) as number}
                        min={(schemaObj.minimum as number | undefined) ?? undefined}
                        max={(schemaObj.maximum as number | undefined) ?? undefined}
                        step={schemaObj.type === 'integer' ? 1 : 0.1}
                        onchange={(e) => updateConfigValue(key, Number((e.target as HTMLInputElement).value))}
                      />
                    {:else if schemaObj.enum}
                      <select
                        id={`config-${key}`}
                        value={configValue(key, schemaObj, '') as string}
                        onchange={(e) => updateConfigValue(key, (e.target as HTMLSelectElement).value)}
                      >
                        {#each (schemaObj.enum as string[]) as option}
                          <option value={option}>{option}</option>
                        {/each}
                      </select>
                    {:else}
                      <input
                        type="text"
                        id={`config-${key}`}
                        value={configValue(key, schemaObj, '') as string}
                        placeholder={schemaObj.placeholder as string || ''}
                        onchange={(e) => updateConfigValue(key, (e.target as HTMLInputElement).value)}
                      />
                    {/if}
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
        <div class="modal-footer">
          <Button variant="ghost" onclick={() => (editingBuiltinTool = null)}>
            Cancel
          </Button>
          {#if tool.customDescription || descriptionInput !== tool.defaultDescription}
            <Button variant="ghost" onclick={resetDescription}>
              Reset to Default
            </Button>
          {/if}
          <Button variant="primary" onclick={saveBuiltinTool} disabled={unifiedToolsStore.loading}>
            Save Changes
          </Button>
        </div>
      </div>
    </div>
  {/if}
</div>

{#snippet authBadge(status: string | null | undefined, provider: string | null | undefined)}
  {#if status === 'needs_setup'}
    <span class="auth-badge needs-setup" data-tooltip={`Provider "${provider}": no credential saved`}>auth required</span>
  {:else if status === 'pending'}
    <span class="auth-badge pending" data-tooltip={`Provider "${provider}": credential setup pending`}>auth pending</span>
  {/if}
{/snippet}

{#snippet builtinRow(gi: GroupedToolItem)}
  {@const tool = gi.data as DefaultToolInfo}
  {@const selected = selectedTools.has(tool.name)}
  <div class="tool-info">
    <span class="tool-name">
      {tool.name}
      {#if isAdminOnlyTool(tool.name)}
        <span class="admin-only-badge" data-tooltip={isAdmin ? "Requires admin role" : "You don't have the admin role. Toggling this tool will work, but the agent will hit 403 when invoking it"}>admin only</span>
      {/if}
      {@render authBadge(tool.auth_status, tool.auth_provider)}
    </span>
    <span class="tool-desc">{tool.description}</span>
  </div>
  <div class="tool-row-actions">
    <button
      class="row-edit-btn"
      onclick={() => openBuiltinEditor(tool.name)}
      type="button"
      data-tooltip="Edit tool"
      aria-label="Edit tool"
    >
      <Icon name="edit" size={14} />
    </button>
    <ToggleSwitch
      checked={selected}
      onclick={() => toggleTool(tool.name)}
      dataTooltip={selected ? 'Remove from defaults' : 'Add to defaults'}
      ariaLabel={`${selected ? 'Remove' : 'Add'} ${tool.name} ${selected ? 'from' : 'to'} default tools`}
    />
  </div>
{/snippet}

{#if showWarning}
  <ToolCountWarning
    toolCount={defaultCount}
    callableCount={defaultToolsStore.callableThreadCount}
    onContinue={doSave}
    onGoBack={() => (showWarning = false)}
  />
{/if}

<style>
  .tool-management {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    flex: 1 1 auto;
    min-height: 0;
  }

  /* Scrollable region inside the Tools tab. Right padding keeps content
     clear of the scrollbar, which itself sits flush with the Settings
     modal's right border (parent reset via .tab-tools-flex margin). */
  .panel-scroll {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding-right: var(--spacing-lg);
  }

  .loading {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
    font-style: italic;
  }

  /* Summary bar */
  .summary-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .summary-left {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .summary-count {
    color: var(--text-secondary);
  }

  .summary-callable {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .mode-badge {
    display: inline-block;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    padding: 1px 6px;
    border-radius: var(--radius-md);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    /* text-indent matches the 0.5px letter-spacing AND adds a small extra
       nudge to compensate for the pill shape's optical-centering pull. */
    text-indent: 1.5px;
  }

  .mode-badge.custom {
    background: var(--accent-tint-bg);
    color: var(--accent-primary);
  }

  .inline-warning {
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--warning) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--warning) 30%, transparent);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--warning);
  }

  /* Search */
  .search-bar {
    padding: 0;
  }

  .search-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .search-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .search-input::placeholder {
    color: var(--text-muted);
  }

  /* Section groups (Default / Available): bordered card with a solid grey header bar */
  .section-group {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  /* Unused for the Default/Available buttons now, kept for any other callers */
  .section-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-xs);
  }

  /* Solid grey title bar for the Default / Available cards */
  button.section-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: 44px;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 0;
    border-bottom: 1px solid var(--border-default);
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    color: var(--text-primary);
    font: inherit;
    text-align: left;
    cursor: pointer;
    appearance: none;
    -webkit-appearance: none;
    transition: background var(--transition-fast);
  }

  button.section-toggle:hover {
    background: var(--bg-hover);
  }

  button.section-toggle:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  /* When collapsed: round all corners, drop the divider */
  .section-group.collapsed button.section-toggle {
    border-bottom: none;
    border-radius: var(--radius-md);
  }

  .section-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    transition: transform 120ms var(--ease-out);
  }

  .section-chevron.open {
    transform: rotate(90deg);
  }

  /* md/600 section-heading tier; still outweighs the CORE/PROFILE category bars below */
  .section-heading {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .section-count {
    margin-left: auto;
    font-size: var(--font-size-xs);
    font-weight: 600;
    padding: 0 6px;
    min-width: 20px;
    text-align: center;
    white-space: nowrap;
    flex-shrink: 0;
    border-radius: var(--radius-full);
    background: var(--accent-tint-bg);
    color: var(--accent-primary);
  }

  .section-hint {
    margin: 0;
    padding: var(--spacing-xs) var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-subtle);
  }

  .section-empty {
    padding: var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    text-align: center;
  }

  .available-section {
    margin-top: var(--spacing-sm);
  }

  .available-section .section-count {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
  }

  /* Body inside a .section-group card: the readable category cards (rendered by
     ToolGroupedList) sit here with a little inset. No inner scroller, so the
     modal body is the only scroll surface. */
  .tools-body {
    padding: var(--spacing-sm);
  }

  .tool-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .tool-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .admin-only-badge {
    display: inline-block;
    font-size: var(--font-size-3xs);
    font-weight: 700;
    padding: 0 5px;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    color: var(--warning);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    text-indent: 0.6px;
    margin-left: 4px;
  }

  /* Credential-axis nudge badge. Warning tone mirrors .admin-only-badge for
     "needs_setup"; the muted tone mirrors the .tt-tag neutral chip for the
     softer "pending" state. Only these two states render (see authBadge). */
  .auth-badge {
    display: inline-block;
    font-size: var(--font-size-3xs);
    font-weight: 700;
    padding: 0 5px;
    border-radius: var(--radius-sm);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    text-indent: 0.6px;
    margin-left: 4px;
  }
  .auth-badge.needs-setup {
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    color: var(--warning);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent);
  }
  .auth-badge.pending {
    background: var(--bg-elevated-2);
    color: var(--text-secondary);
    border: 1px solid var(--border-subtle);
  }

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Row actions — pen edit button + toggle */
  .tool-row-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-shrink: 0;
  }

  .row-edit-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    background: none;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast);
  }

  .row-edit-btn:hover {
    color: var(--text-primary);
    border-color: var(--border-subtle);
    background: var(--bg-elevated);
  }

  /* Footer */
  .panel-footer {
    display: flex;
    justify-content: space-between;
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  /* Pinned variant: stays at the bottom of the Tools tab regardless
     of scroll position. Right padding matches .panel-scroll so the
     Save button aligns with the scrollable content above. */
  .panel-footer-pinned {
    flex-shrink: 0;
    padding: var(--spacing-sm) var(--spacing-lg) var(--spacing-sm) 0;
    margin-top: 0;
  }

  .save-message {
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    text-align: center;
  }

  .save-message.success {
    background: color-mix(in srgb, var(--success) 15%, transparent);
    color: var(--success);
  }

  .save-message.error {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
  }

  /* Custom tools divider */
  .custom-tools-divider {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    margin-top: var(--spacing-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    font-weight: 600;
  }

  .custom-tools-divider::before,
  .custom-tools-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border-default);
  }

  .custom-tools-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .custom-tools-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--spacing-md);
  }

  .header-description {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .error-message {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .error-message button {
    margin-left: auto;
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    text-decoration: underline;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-lg) var(--spacing-md);
    text-align: center;
  }

  .empty-icon {
    font-size: 36px;
    margin-bottom: var(--spacing-sm);
    opacity: 0.6;
    width: 48px;
    height: 48px;
    border: 2px dashed var(--border-subtle);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
  }

  .empty-state h3 {
    margin: 0 0 var(--spacing-xs);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .empty-message {
    margin: 0 0 var(--spacing-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    max-width: 300px;
  }

  /* Custom tool items */
  .tool-list {
    display: flex;
    flex-direction: column;
  }

  .tool-item {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: all var(--transition-fast);
    margin-bottom: var(--spacing-sm);
  }

  /* The row is a non-interactive container (no onclick); the hover border
     lift is just a "pointing here" cue. Guard it so a toggled-off tool's
     row doesn't light up as if it were active. */
  .tool-item:not(.disabled):hover {
    border-color: var(--border-default);
  }

  .tool-item.disabled {
    opacity: 0.6;
  }

  .tool-item .tool-info {
    flex: 1;
    min-width: 0;
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
  }

  .tool-type {
    padding: 2px 6px;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-transform: uppercase;
  }

  .tool-type.http {
    background: var(--accent-tint-bg);
    color: var(--accent-primary);
  }

  .tool-description {
    margin: 0 0 var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  .tool-meta {
    display: flex;
    gap: var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .tool-actions {
    display: flex;
    gap: var(--spacing-xs);
    margin-left: var(--spacing-md);
  }

  .action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--accent-primary);
  }

  .action-btn.enabled {
    color: var(--success);
  }

  .action-btn.delete:hover {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    border-color: var(--error);
  }

  /* Modal styles */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-md);
    z-index: 1000;
  }

  .modal-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .modal {
    position: relative;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    width: 100%;
    max-width: 600px;
    max-height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 0;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .modal-body {
    padding: var(--spacing-md);
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
  }

  .modal-body p {
    margin: var(--spacing-md) 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .close-btn {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 24px;
    cursor: pointer;
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  /* Form elements */
  .form-group {
    margin-bottom: var(--spacing-md);
  }

  .form-group label {
    display: block;
    margin-bottom: var(--spacing-xs);
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .form-group label .required {
    color: var(--error);
    margin-left: 2px;
  }

  .form-group input[type="text"],
  .form-group input[type="number"],
  .form-group textarea,
  .form-group select {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: inherit;
  }

  .form-group input:focus,
  .form-group textarea:focus,
  .form-group select:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .form-group textarea {
    resize: vertical;
    min-height: 80px;
  }

  .field-description {
    margin: 0 0 var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-weight: normal;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-weight: normal;
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .info-box {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--info) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--info) 20%, transparent);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    margin-bottom: var(--spacing-md);
  }

  .config-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .tool-meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    margin-bottom: var(--spacing-md);
  }

  .meta-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .meta-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .meta-value {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  code.meta-value {
    font-family: var(--font-mono);
  }

  .docstring-readonly {
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    white-space: pre-wrap;
    margin: 0;
  }

  .config-section {
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
    margin-top: var(--spacing-sm);
  }

  .config-section .field-label {
    display: block;
    margin-bottom: var(--spacing-sm);
    font-weight: 600;
    /* §4 — labels recede behind the input value (which is --text-primary),
       so the eye finds the answer before the question. Weight stays at 600
       because these label larger config blocks, not single fields. */
    color: var(--text-secondary);
  }
</style>
