<script lang="ts">
  import { onMount } from 'svelte';
  import { toolsStore } from '$lib/stores/tools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import type { CustomTool, CustomToolCreateRequest, UnifiedTool, DefaultToolInfo } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import ToggleSwitch from '../common/ToggleSwitch.svelte';
  import ToolForm from './ToolForm.svelte';
  import ToolTestPanel from './ToolTestPanel.svelte';
  import ToolCountWarning from './ToolCountWarning.svelte';

  // --- Default tools state (absorbed from DefaultToolsPanel) ---
  let selectedTools = $state<Set<string>>(new Set());
  let initialized = $state(false);
  let searchQuery = $state('');
  let showWarning = $state(false);
  let saveMessage = $state('');
  let saveStatus = $state<'idle' | 'success' | 'error'>('idle');

  // Category display info
  const CATEGORY_INFO: Record<string, { name: string; icon: string }> = {
    core: { name: 'Core', icon: 'terminal' },
    profile: { name: 'Profile', icon: 'brain' },
    notepad: { name: 'Notepad', icon: 'sticky-note' },
    todo: { name: 'TODOs', icon: 'list' },
    self_modify: { name: 'Self-Modify', icon: 'code' },
    subagent: { name: 'Utilities', icon: 'refresh' },
    trigger: { name: 'Triggers', icon: 'zap' },
    email: { name: 'Outlook Email', icon: 'mail' },
    browser: { name: 'Browser', icon: 'globe' },
    calendar: { name: 'Google Calendar', icon: 'calendar' },
    skills: { name: 'Agent Skills', icon: 'bolt' },
    custom: { name: 'Custom', icon: 'puzzle' },
  };

  const CATEGORY_ORDER = ['core', 'profile', 'notepad', 'todo', 'trigger', 'email', 'browser', 'calendar', 'skills', 'self_modify', 'subagent', 'custom'];

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

  // Collapsible state for Core / Available sections
  let coreOpen = $state(true);
  let availableOpen = $state(false);

  // While a search is active, force both sections open so matches are
  // visible regardless of the user's manual collapse state. When the
  // search clears, their original preferences take effect again.
  const isSearching = $derived(searchQuery.trim().length > 0);
  const effectiveCoreOpen = $derived(coreOpen || isSearching);
  const effectiveAvailableOpen = $derived(availableOpen || isSearching);

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
    if (!searchQuery.trim()) return visibleTools;
    const q = searchQuery.toLowerCase();
    return visibleTools.filter(
      (t: DefaultToolInfo) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q)
    );
  });

  // Split into core (selected) and available (not selected), grouped by category
  const coreToolsByCategory = $derived.by(() => {
    const result: Record<string, DefaultToolInfo[]> = {};
    for (const tool of filteredTools) {
      if (selectedTools.has(tool.name)) {
        if (!result[tool.category]) result[tool.category] = [];
        result[tool.category].push(tool);
      }
    }
    return result;
  });

  const availableToolsByCategory = $derived.by(() => {
    const result: Record<string, DefaultToolInfo[]> = {};
    for (const tool of filteredTools) {
      if (!selectedTools.has(tool.name)) {
        if (!result[tool.category]) result[tool.category] = [];
        result[tool.category].push(tool);
      }
    }
    return result;
  });

  const visibleDefaultToolCount = $derived(defaultToolsStore.tools.filter((tool) => !isMcpDefaultTool(tool)).length);
  const coreCount = $derived(
    defaultToolsStore.tools.filter((tool) => !isMcpDefaultTool(tool) && selectedTools.has(tool.name)).length
  );
  const availableCount = $derived(visibleDefaultToolCount - coreCount);
  const totalWithCallable = $derived(coreCount + defaultToolsStore.callableThreadCount);

  // Match counts within the current search, per section. Used to show
  // "no results" hints inside sections the search failed to find anything in.
  const coreMatchCount = $derived(
    Object.values(coreToolsByCategory).reduce((n, arr) => n + arr.length, 0)
  );
  const availableMatchCount = $derived(
    Object.values(availableToolsByCategory).reduce((n, arr) => n + arr.length, 0)
  );

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
      saveStatus = 'success';
      saveMessage = 'Core tool set saved!';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || 'Failed to save';
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
      selectedTools = new Set(mergedDefaults);
      saveStatus = 'success';
      saveMessage = 'Reset to Nymeria defaults';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || 'Failed to reset';
    }
    setTimeout(() => { saveMessage = ''; saveStatus = 'idle'; }, 3000);
  }

  function getCategoryInfo(category: string) {
    return CATEGORY_INFO[category] || { name: category, icon: 'tool' };
  }

  // --- Custom tools handlers ---
  const filteredCustomTools = $derived.by(() => {
    let result = toolsStore.tools;
    if (customFilter !== 'all') {
      result = result.filter((t) => t.implementationType === customFilter);
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          t.description.toLowerCase().includes(q) ||
          t.id.toLowerCase().includes(q)
      );
    }
    return result;
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

    const hasConfigSchema = tool.configSchema && Object.keys(tool.configSchema).length > 0;
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

<div class="tool-management">
  <div class="panel-scroll">
  {#if defaultToolsStore.loading}
    <div class="loading">Loading tools...</div>
  {:else if !defaultToolsStore.loaded}
    <div class="loading">Connect to the API to configure tools.</div>
  {:else}
    <!-- Summary bar -->
    <div class="summary-bar">
      <div class="summary-left">
        <span class="summary-count">
          <strong>{coreCount}</strong> core tools for new threads
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
        placeholder="Search tools..."
      />
    </div>

    <!-- Core Tools Section -->
    {#if coreCount > 0}
      <div class="section-group" class:collapsed={!effectiveCoreOpen}>
        <button
          class="section-header section-toggle"
          onclick={() => (coreOpen = !coreOpen)}
          type="button"
          aria-expanded={effectiveCoreOpen}
        >
          <span class="section-chevron" class:open={effectiveCoreOpen}>
            <Icon name="chevronRight" size={14} />
          </span>
          <span class="section-title">Core Tools</span>
          <span class="section-count">{isSearching ? `${coreMatchCount}/${coreCount}` : coreCount}</span>
        </button>
        {#if effectiveCoreOpen}
        <p class="section-hint">Loaded automatically in every new thread. Toggle off to move to Available.</p>
        {#if isSearching && coreMatchCount === 0}
          <div class="section-empty">No core tools match "{searchQuery}".</div>
        {/if}
        <div class="tools-list">
          {#each CATEGORY_ORDER as category}
            {#if coreToolsByCategory[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = coreToolsByCategory[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row selected">
                      <div class="tool-info">
                        <span class="tool-name">
                          {tool.name}
                          {#if isAdminOnlyTool(tool.name)}
                            <span class="admin-only-badge" title={isAdmin ? "Requires admin role" : "You don't have the admin role. Toggling this tool will work, but the agent will hit 403 when invoking it"}>admin only</span>
                          {/if}
                        </span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <div class="tool-row-actions">
                        <button
                          class="row-edit-btn"
                          onclick={() => openBuiltinEditor(tool.name)}
                          type="button"
                          title="Edit tool"
                        >
                          <Icon name="edit" size={14} />
                        </button>
                        <ToggleSwitch
                          checked={true}
                          onclick={() => toggleTool(tool.name)}
                          title="Remove from core"
                          ariaLabel={`Remove ${tool.name} from core tools`}
                        />
                      </div>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}

          <!-- Categories not in CATEGORY_ORDER -->
          {#each Object.keys(coreToolsByCategory) as category}
            {#if !CATEGORY_ORDER.includes(category) && coreToolsByCategory[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = coreToolsByCategory[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row selected">
                      <div class="tool-info">
                        <span class="tool-name">
                          {tool.name}
                          {#if isAdminOnlyTool(tool.name)}
                            <span class="admin-only-badge" title={isAdmin ? "Requires admin role" : "You don't have the admin role. Toggling this tool will work, but the agent will hit 403 when invoking it"}>admin only</span>
                          {/if}
                        </span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <div class="tool-row-actions">
                        <button
                          class="row-edit-btn"
                          onclick={() => openBuiltinEditor(tool.name)}
                          type="button"
                          title="Edit tool"
                        >
                          <Icon name="edit" size={14} />
                        </button>
                        <ToggleSwitch
                          checked={true}
                          onclick={() => toggleTool(tool.name)}
                          title="Remove from core"
                          ariaLabel={`Remove ${tool.name} from core tools`}
                        />
                      </div>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}
        </div>
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
          <span class="section-title">Available Tools</span>
          <span class="section-count">{isSearching ? `${availableMatchCount}/${availableCount}` : availableCount}</span>
        </button>
        {#if effectiveAvailableOpen}
        <p class="section-hint">Not loaded by default. Toggle on to promote to Core, or enable per-thread in thread settings.</p>
        {#if isSearching && availableMatchCount === 0}
          <div class="section-empty">No available tools match "{searchQuery}".</div>
        {/if}
        <div class="tools-list">
          {#each CATEGORY_ORDER as category}
            {#if availableToolsByCategory[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = availableToolsByCategory[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row" class:optional={tool.is_optional}>
                      <div class="tool-info">
                        <span class="tool-name">
                          {tool.name}
                          {#if tool.is_optional}
                            <span class="optional-badge">optional</span>
                          {/if}
                          {#if isAdminOnlyTool(tool.name)}
                            <span class="admin-only-badge" title={isAdmin ? "Requires admin role" : "You don't have the admin role. Toggling this tool will work, but the agent will hit 403 when invoking it"}>admin only</span>
                          {/if}
                        </span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <div class="tool-row-actions">
                        <button
                          class="row-edit-btn"
                          onclick={() => openBuiltinEditor(tool.name)}
                          type="button"
                          title="Edit tool"
                        >
                          <Icon name="edit" size={14} />
                        </button>
                        <ToggleSwitch
                          checked={false}
                          onclick={() => toggleTool(tool.name)}
                          title="Add to core"
                          ariaLabel={`Add ${tool.name} to core tools`}
                        />
                      </div>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}

          <!-- Categories not in CATEGORY_ORDER -->
          {#each Object.keys(availableToolsByCategory) as category}
            {#if !CATEGORY_ORDER.includes(category) && availableToolsByCategory[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = availableToolsByCategory[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row">
                      <div class="tool-info">
                        <span class="tool-name">{tool.name}</span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <div class="tool-row-actions">
                        <button
                          class="row-edit-btn"
                          onclick={() => openBuiltinEditor(tool.name)}
                          type="button"
                          title="Edit tool"
                        >
                          <Icon name="edit" size={14} />
                        </button>
                        <ToggleSwitch
                          checked={false}
                          onclick={() => toggleTool(tool.name)}
                          title="Add to core"
                          ariaLabel={`Add ${tool.name} to core tools`}
                        />
                      </div>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}
        </div>
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
        <button onclick={() => toolsStore.clearError()}>Dismiss</button>
      </div>
    {/if}

    {#if toolsStore.loading}
      <div class="loading">Loading custom tools...</div>
    {:else if filteredCustomTools.length === 0}
      <div class="empty-state">
        <div class="empty-icon">+</div>
        <h4>No Custom Tools Yet</h4>
        <p class="empty-message">
          Create HTTP or MCP tools to extend your assistant's capabilities.
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
                title="Test tool"
                onclick={() => (testingTool = tool)}
              >
                <Icon name="play" size={16} />
              </button>
              <button
                class="action-btn"
                title="Edit tool"
                onclick={() => (editingTool = tool)}
              >
                <Icon name="edit" size={16} />
              </button>
              <button
                class="action-btn"
                class:enabled={tool.enabled}
                title={tool.enabled ? 'Disable' : 'Enable'}
                onclick={() => handleToggleEnabled(tool)}
              >
                <Icon name={tool.enabled ? 'visible' : 'hidden'} size={16} />
              </button>
              <button
                class="action-btn delete"
                title="Delete tool"
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

  <!-- Pinned footer: Save/Reset for the core-tools selection -->
  {#if defaultToolsStore.loaded}
    <div class="panel-footer panel-footer-pinned">
      <button
        class="btn btn-ghost"
        onclick={handleReset}
        disabled={defaultToolsStore.saving}
        type="button"
      >
        Reset to Nymeria Defaults
      </button>
      <button
        class="btn btn-primary"
        onclick={handleSave}
        disabled={defaultToolsStore.saving || !hasChanges}
        type="button"
      >
        {defaultToolsStore.saving ? 'Saving...' : 'Save Changes'}
      </button>
    </div>

    {#if saveMessage}
      <div class="save-message" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
        {saveMessage}
      </div>
    {/if}
  {/if}

  <!-- Create form modal -->
  {#if showCreateForm}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) showCreateForm = false; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal">
        <div class="modal-header">
          <h3>Create Custom Tool</h3>
          <button class="close-btn" onclick={() => (showCreateForm = false)}>
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
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) editingTool = null; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal">
        <div class="modal-header">
          <h3>Edit Tool: {editingTool.name}</h3>
          <button class="close-btn" onclick={() => (editingTool = null)}>
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
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) testingTool = null; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal">
        <div class="modal-header">
          <h3>Test Tool: {testingTool.name}</h3>
          <button class="close-btn" onclick={() => (testingTool = null)}>
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
    {@const hasConfigSchema = tool.configSchema && Object.keys(tool.configSchema).length > 0}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) editingBuiltinTool = null; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal">
        <div class="modal-header">
          <h3>Edit Tool: {tool.name}</h3>
          <button class="close-btn" onclick={() => (editingBuiltinTool = null)}>
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
              placeholder="Enter a custom description..."
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
                {#each Object.entries(tool.configSchema ?? {}) as [key, schema]}
                  {@const schemaObj = schema as Record<string, unknown>}
                  <div class="form-group">
                    <label for={`config-${key}`}>
                      {schemaObj.title || key}
                      {#if schemaObj.required}
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
                          checked={configInputs[key] as boolean || false}
                          onchange={(e) => updateConfigValue(key, (e.target as HTMLInputElement).checked)}
                        />
                        <span>Enabled</span>
                      </label>
                    {:else if schemaObj.type === 'number' || schemaObj.type === 'integer'}
                      <input
                        type="number"
                        id={`config-${key}`}
                        value={configInputs[key] as number || schemaObj.default || 0}
                        min={schemaObj.minimum as number || undefined}
                        max={schemaObj.maximum as number || undefined}
                        step={schemaObj.type === 'integer' ? 1 : 0.1}
                        onchange={(e) => updateConfigValue(key, parseFloat((e.target as HTMLInputElement).value))}
                      />
                    {:else if schemaObj.enum}
                      <select
                        id={`config-${key}`}
                        value={configInputs[key] as string || schemaObj.default || ''}
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
                        value={configInputs[key] as string || schemaObj.default || ''}
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

{#if showWarning}
  <ToolCountWarning
    toolCount={coreCount}
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
    font-size: 10px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: var(--radius-full);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .mode-badge.custom {
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
    color: var(--accent-primary);
  }

  .inline-warning {
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--warning, #f59e0b) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--warning, #f59e0b) 30%, transparent);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--warning, #f59e0b);
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
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .search-input::placeholder {
    color: var(--text-muted);
  }

  /* Section groups (Core / Available) — bordered card with a solid grey header bar */
  .section-group {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  /* Unused for the Core/Available buttons now, kept for any other callers */
  .section-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-xs);
  }

  /* Solid grey title bar for the Core / Available cards */
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
    transition: transform var(--transition-normal) cubic-bezier(0.4, 0, 0.2, 1);
  }

  .section-chevron.open {
    transform: rotate(90deg);
  }

  /* Promote the section title so it outweighs the CORE/PROFILE category bars below */
  .section-toggle .section-title {
    font-size: var(--font-size-md, 14px);
    font-weight: 700;
    letter-spacing: 0.01em;
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .section-count {
    font-size: var(--font-size-xs);
    font-weight: 600;
    padding: 0 6px;
    min-width: 20px;
    text-align: center;
    border-radius: var(--radius-full);
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
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
    font-style: italic;
    text-align: center;
  }

  .available-section {
    margin-top: var(--spacing-sm);
  }

  .available-section .section-count {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
  }

  /* Tools list — sits inside .section-group card, so it inherits the outer border */
  .tools-list {
    max-height: 320px;
    overflow-y: auto;
  }

  .category-group {
    border-bottom: 1px solid var(--border-default);
  }

  .category-group:last-child {
    border-bottom: none;
  }

  .category-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-md);
    background: var(--bg-elevated-2);
    font-size: var(--font-size-xs);
  }

  .category-label .category-name {
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }

  .category-label .category-count {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 6px var(--spacing-md) 6px calc(var(--spacing-md) + 8px);
    border-bottom: 1px solid var(--border-subtle, var(--border-default));
    transition: opacity var(--transition-fast);
  }

  .tool-row:last-child {
    border-bottom: none;
  }

  .tool-row:not(.selected) {
    opacity: 0.6;
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

  .optional-badge {
    font-size: 9px;
    font-weight: 600;
    padding: 0 4px;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--accent-secondary, #818cf8) 15%, transparent);
    color: var(--accent-secondary, #818cf8);
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }

  .admin-only-badge {
    font-size: 9px;
    font-weight: 700;
    padding: 0 5px;
    border-radius: var(--radius-sm);
    background: rgba(251, 191, 36, 0.12);
    color: var(--warning, #fbbf24);
    border: 1px solid rgba(251, 191, 36, 0.4);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-left: 4px;
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
    border-radius: var(--radius-sm, 4px);
    color: var(--text-muted);
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast);
  }

  .row-edit-btn:hover {
    color: var(--text-primary);
    border-color: var(--border-subtle);
    background: var(--bg-secondary);
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

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-ghost:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .save-message {
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    text-align: center;
  }

  .save-message.success {
    background: rgba(52, 211, 153, 0.15);
    color: var(--success);
  }

  .save-message.error {
    background: rgba(248, 113, 113, 0.15);
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
    background: rgba(248, 113, 113, 0.15);
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

  .empty-state h4 {
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
    transition: all 0.15s ease;
    margin-bottom: var(--spacing-sm);
  }

  .tool-item:hover {
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
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-transform: uppercase;
  }

  .tool-type.http {
    background: rgba(34, 211, 238, 0.15);
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
    background: var(--bg-elevated-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s ease;
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
    background: rgba(248, 113, 113, 0.15);
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

  .modal {
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

  .modal-header h3 {
    margin: 0;
    color: var(--text-primary);
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
    transition: all 0.15s ease;
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
    background: rgba(99, 102, 241, 0.1);
    border: 1px solid rgba(99, 102, 241, 0.2);
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
    background: var(--bg-secondary);
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
    font-size: var(--font-size-xs, 11px);
    text-transform: uppercase;
    letter-spacing: 0.05em;
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
    font-family: var(--font-mono, monospace);
  }

  .docstring-readonly {
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-secondary);
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
  }
</style>
