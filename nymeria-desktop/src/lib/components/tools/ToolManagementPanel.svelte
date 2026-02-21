<script lang="ts">
  import { toolsStore } from '$lib/stores/tools.svelte';
  import { builtInToolsStore } from '$lib/stores/builtInTools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import type { CustomTool, CustomToolCreateRequest, BuiltInTool, ToolCategory, UnifiedTool } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import ToolForm from './ToolForm.svelte';
  import ToolTestPanel from './ToolTestPanel.svelte';

  // UI State
  let showCreateForm = $state(false);
  let editingTool = $state<CustomTool | null>(null);
  let testingTool = $state<CustomTool | null>(null);
  let filter = $state<'all' | 'http' | 'mcp'>('all');
  let searchQuery = $state('');
  let activeTab = $state<'builtin' | 'custom'>('builtin');
  let expandedCategories = $state<Set<string>>(new Set(['core', 'memory', 'todo']));

  // Unified view state
  let useUnifiedView = $state(true);
  let categoryFilter = $state<string | null>(null);

  // Tool editing state (for description and config)
  let editingDescriptionTool = $state<UnifiedTool | null>(null);
  let editingConfigTool = $state<UnifiedTool | null>(null);
  let descriptionInput = $state('');
  let configInputs = $state<Record<string, unknown>>({});

  // Load tools on mount
  $effect(() => {
    if (useUnifiedView) {
      if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) {
        unifiedToolsStore.loadTools();
      }
    } else {
      if (!toolsStore.loaded && !toolsStore.loading) {
        toolsStore.loadTools();
      }
      if (!builtInToolsStore.loaded && !builtInToolsStore.loading) {
        builtInToolsStore.loadTools();
      }
    }
  });

  // Filtered unified tools
  const filteredUnifiedTools = $derived(() => {
    let result = unifiedToolsStore.tools;

    // Filter by category
    if (categoryFilter) {
      result = result.filter((t) => t.category === categoryFilter);
    }

    // Filter by search
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

  // Group unified tools by category
  const unifiedToolsByCategory = $derived(() => {
    const result: Record<string, UnifiedTool[]> = {};
    for (const tool of filteredUnifiedTools()) {
      const cat = tool.category;
      if (!result[cat]) {
        result[cat] = [];
      }
      result[cat].push(tool);
    }
    return result;
  });

  // Filtered custom tools
  const filteredTools = $derived(() => {
    let result = toolsStore.tools;

    // Filter by type
    if (filter !== 'all') {
      result = result.filter((t) => t.implementationType === filter);
    }

    // Filter by search
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

  // Filtered built-in tools
  const filteredBuiltInTools = $derived(() => {
    if (!searchQuery) return builtInToolsStore.byCategory;

    const q = searchQuery.toLowerCase();
    const filtered: Record<string, BuiltInTool[]> = {};

    for (const [category, categoryTools] of Object.entries(builtInToolsStore.byCategory)) {
      const matching = categoryTools.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          t.description.toLowerCase().includes(q)
      );
      if (matching.length > 0) {
        filtered[category] = matching;
      }
    }

    return filtered as Record<ToolCategory, BuiltInTool[]>;
  });

  // Category order for display (subagent excluded - managed in Sub-Agents tab)
  const categoryOrder: ToolCategory[] = ['core', 'memory', 'todo', 'self_modify'];

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

  async function handleBuiltInToggle(tool: BuiltInTool) {
    await builtInToolsStore.setToolEnabled(tool.name, !tool.enabled);
  }

  async function handleCategoryToggle(category: string, currentlyEnabled: boolean) {
    await builtInToolsStore.setCategoryEnabled(category, !currentlyEnabled);
  }

  function toggleCategory(category: string) {
    const newExpanded = new Set(expandedCategories);
    if (newExpanded.has(category)) {
      newExpanded.delete(category);
    } else {
      newExpanded.add(category);
    }
    expandedCategories = newExpanded;
  }

  function isCategoryEnabled(category: string): boolean {
    const tools = builtInToolsStore.byCategory[category as ToolCategory] || [];
    if (tools.length === 0) return true;
    // Category is enabled if not in disabled list
    const prefs = builtInToolsStore.preferences;
    if (!prefs) return true;
    return !prefs.disabledCategories.includes(category);
  }

  function formatDate(date: Date): string {
    return new Intl.DateTimeFormat('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    }).format(date);
  }

  function getSecurityBadge(level: string): { text: string; class: string } {
    switch (level) {
      case 'sensitive':
        return { text: 'SENSITIVE', class: 'badge-sensitive' };
      case 'moderate':
        return { text: 'MODERATE', class: 'badge-moderate' };
      default:
        return { text: 'SAFE', class: 'badge-safe' };
    }
  }

  async function handleResetToDefaults() {
    if (confirm('Reset all tool preferences to defaults? This will clear all customizations.')) {
      await builtInToolsStore.resetToDefaults();
    }
  }

  // Unified tool handlers
  async function handleUnifiedToggle(tool: UnifiedTool) {
    await unifiedToolsStore.setToolEnabled(tool.id, !tool.enabled);
  }

  async function handleUnifiedDelete(tool: UnifiedTool) {
    if (!tool.editable) return;
    if (confirm(`Are you sure you want to delete "${tool.name}"?`)) {
      await unifiedToolsStore.deleteCustomTool(tool.id);
    }
  }

  function getToolTypeBadge(tool: UnifiedTool): { text: string; class: string } {
    if (tool.toolType === 'builtin') {
      return { text: 'SYSTEM', class: 'badge-system' };
    } else if (tool.implementationType === 'http') {
      return { text: 'HTTP', class: 'badge-http' };
    } else if (tool.implementationType === 'mcp') {
      return { text: 'MCP', class: 'badge-mcp' };
    }
    return { text: 'CUSTOM', class: 'badge-custom' };
  }

  // Description editing handlers
  function openDescriptionEditor(tool: UnifiedTool) {
    editingDescriptionTool = tool;
    descriptionInput = tool.customDescription || tool.defaultDescription;
  }

  async function saveDescription() {
    if (!editingDescriptionTool) return;

    // If input is same as default, clear custom description
    const newDescription = descriptionInput.trim() === editingDescriptionTool.defaultDescription
      ? null
      : descriptionInput.trim();

    await unifiedToolsStore.setToolDescription(editingDescriptionTool.id, newDescription);
    editingDescriptionTool = null;
    descriptionInput = '';
  }

  function resetDescription() {
    if (editingDescriptionTool) {
      descriptionInput = editingDescriptionTool.defaultDescription;
    }
  }

  // Config editing handlers
  function openConfigEditor(tool: UnifiedTool) {
    editingConfigTool = tool;
    configInputs = { ...tool.userConfig };
  }

  async function saveConfig() {
    if (!editingConfigTool) return;
    await unifiedToolsStore.setToolConfig(editingConfigTool.id, configInputs);
    editingConfigTool = null;
    configInputs = {};
  }

  function updateConfigValue(key: string, value: unknown) {
    configInputs = { ...configInputs, [key]: value };
  }

  // Category order for unified view
  const unifiedCategoryOrder = ['core', 'memory', 'todo', 'subagent', 'self_modify', 'custom'];
</script>

<div class="tool-management">
  {#if useUnifiedView}
    <!-- Unified Tools View -->
    <div class="unified-header">
      <div class="header-info">
        <h3>All Tools</h3>
        <p class="header-description">
          Manage all tools in one place. System tools are built-in, custom tools are user-created.
        </p>
      </div>
      <div class="header-actions">
        <Button variant="ghost" onclick={handleResetToDefaults}>
          Reset to Defaults
        </Button>
        <Button variant="primary" onclick={() => (showCreateForm = true)}>
          + New Tool
        </Button>
      </div>
    </div>

    <!-- Search and filters -->
    <div class="unified-filters">
      <div class="search">
        <input
          type="text"
          placeholder="Search all tools..."
          bind:value={searchQuery}
        />
      </div>
      <div class="category-filters">
        <button
          class="category-chip"
          class:active={categoryFilter === null}
          onclick={() => (categoryFilter = null)}
        >
          All ({unifiedToolsStore.tools.length})
        </button>
        {#each unifiedCategoryOrder as cat}
          {@const catTools = unifiedToolsStore.toolsByCategory[cat] || []}
          {#if catTools.length > 0}
            {@const info = unifiedToolsStore.getCategoryInfo(cat)}
            <button
              class="category-chip"
              class:active={categoryFilter === cat}
              onclick={() => (categoryFilter = categoryFilter === cat ? null : cat)}
            >
              {info.name} ({catTools.length})
            </button>
          {/if}
        {/each}
      </div>
    </div>

    <!-- Stats row -->
    <div class="stats-row">
      <span class="stat">
        <strong>{unifiedToolsStore.builtinCount}</strong> system tools
      </span>
      <span class="stat">
        <strong>{unifiedToolsStore.customCount}</strong> custom tools
      </span>
      <span class="stat">
        <strong>{unifiedToolsStore.enabledTools.length}</strong> enabled
      </span>
    </div>

    <!-- Error message -->
    {#if unifiedToolsStore.error}
      <div class="error-message">
        <Icon name="error" size={16} />
        {unifiedToolsStore.error}
        <button onclick={() => unifiedToolsStore.clearError()}>Dismiss</button>
      </div>
    {/if}

    <!-- Loading state -->
    {#if unifiedToolsStore.loading && !unifiedToolsStore.loaded}
      <div class="loading">Loading tools...</div>
    {:else if filteredUnifiedTools().length === 0}
      <div class="empty-state">
        {#if searchQuery || categoryFilter}
          <p class="empty-message">No tools match your filters.</p>
          <button class="clear-filters" onclick={() => { searchQuery = ''; categoryFilter = null; }}>
            Clear filters
          </button>
        {:else}
          <div class="empty-icon">+</div>
          <h4>No Tools Found</h4>
          <p class="empty-message">Create custom tools to extend your assistant's capabilities.</p>
          <Button variant="primary" onclick={() => (showCreateForm = true)}>
            + Create Your First Tool
          </Button>
        {/if}
      </div>
    {:else}
      <!-- Tools by category -->
      <div class="unified-categories">
        {#each unifiedCategoryOrder as category}
          {@const categoryTools = unifiedToolsByCategory()[category] || []}
          {@const info = unifiedToolsStore.getCategoryInfo(category)}
          {@const isExpanded = expandedCategories.has(category)}

          {#if categoryTools.length > 0}
            <div class="category">
              <button
                class="category-header"
                onclick={() => toggleCategory(category)}
              >
                <div class="category-info">
                  <Icon name={isExpanded ? 'chevron-down' : 'chevron-right'} size={16} />
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">({categoryTools.length})</span>
                </div>
              </button>

              {#if isExpanded}
                <div class="category-description">{info.description}</div>
                <div class="tool-list">
                  {#each categoryTools as tool (tool.id)}
                    {@const badge = getSecurityBadge(tool.securityLevel)}
                    {@const typeBadge = getToolTypeBadge(tool)}
                    <div class="unified-tool-item" class:disabled={!tool.enabled}>
                      <div class="tool-info">
                        <div class="tool-header">
                          <span class="tool-name">{tool.name}</span>
                          <span class="type-badge {typeBadge.class}">{typeBadge.text}</span>
                          <span class="security-badge {badge.class}">{badge.text}</span>
                          {#if tool.customDescription}
                            <span class="custom-badge">Custom Description</span>
                          {/if}
                        </div>
                        <p class="tool-description">{tool.description}</p>
                        {#if tool.enabledReason !== 'default'}
                          <span class="enabled-reason">
                            {#if tool.enabledReason === 'user_override'}
                              User override
                            {:else if tool.enabledReason === 'category_disabled'}
                              Category disabled
                            {/if}
                          </span>
                        {/if}
                      </div>
                      <div class="tool-actions">
                        <!-- Edit description button (available for all tools) -->
                        <button
                          class="action-btn edit-desc"
                          title="Edit description"
                          onclick={() => openDescriptionEditor(tool)}
                        >
                          <Icon name="textEdit" size={16} />
                        </button>
                        <!-- Configure button (only for configurable tools) -->
                        {#if tool.configurable && tool.configSchema}
                          <button
                            class="action-btn configure"
                            title="Configure"
                            onclick={() => openConfigEditor(tool)}
                          >
                            <Icon name="cog" size={16} />
                          </button>
                        {/if}
                        {#if tool.editable}
                          <button
                            class="action-btn"
                            title="Edit tool"
                            onclick={() => {
                              // Convert to CustomTool for editing
                              editingTool = {
                                id: tool.id,
                                name: tool.name,
                                description: tool.description,
                                implementationType: tool.implementationType || 'http',
                                parameters: tool.parameters ? Object.values(tool.parameters) : [],
                                http: tool.httpConfig,
                                mcp: tool.mcpConfig,
                                tags: tool.tags,
                                enabled: tool.enabled,
                                createdAt: tool.createdAt ? new Date(tool.createdAt) : new Date(),
                                updatedAt: tool.updatedAt ? new Date(tool.updatedAt) : new Date()
                              } as unknown as CustomTool;
                            }}
                          >
                            <Icon name="edit" size={16} />
                          </button>
                          <button
                            class="action-btn delete"
                            title="Delete tool"
                            onclick={() => handleUnifiedDelete(tool)}
                          >
                            <Icon name="trash" size={16} />
                          </button>
                        {/if}
                        <label class="toggle-switch">
                          <input
                            type="checkbox"
                            checked={tool.enabled}
                            onchange={() => handleUnifiedToggle(tool)}
                            disabled={unifiedToolsStore.loading}
                          />
                          <span class="toggle-slider"></span>
                        </label>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}
        {/each}
      </div>
    {/if}

  {:else}
    <!-- Legacy Tab View (kept for backwards compatibility) -->
    <!-- Tab switcher -->
    <div class="tabs">
      <button
        class="tab"
        class:active={activeTab === 'builtin'}
        onclick={() => (activeTab = 'builtin')}
      >
        Built-in Tools
      </button>
      <button
        class="tab"
        class:active={activeTab === 'custom'}
        onclick={() => (activeTab = 'custom')}
      >
        Custom Tools
      </button>
    </div>
  {/if}

  {#if activeTab === 'builtin'}
    <!-- Built-in Tools Section -->
    <div class="section">
      <div class="header">
        <div class="header-info">
          <p class="header-description">
            Enable or disable built-in tools.
          </p>
        </div>
        <div class="header-actions">
          <Button variant="ghost" onclick={handleResetToDefaults}>
            Reset to Defaults
          </Button>
        </div>
      </div>

      <!-- Search -->
      <div class="search">
        <input
          type="text"
          placeholder="Search built-in tools..."
          bind:value={searchQuery}
        />
      </div>

      <!-- Error message -->
      {#if builtInToolsStore.error}
        <div class="error-message">
          <Icon name="error" size={16} />
          {builtInToolsStore.error}
          <button onclick={() => builtInToolsStore.clearError()}>Dismiss</button>
        </div>
      {/if}

      <!-- Loading state -->
      {#if builtInToolsStore.loading && !builtInToolsStore.loaded}
        <div class="loading">Loading built-in tools...</div>
      {:else}
        <!-- Categories -->
        <div class="categories">
          {#each categoryOrder as category}
            {@const categoryTools = filteredBuiltInTools()[category] || []}
            {@const info = builtInToolsStore.getCategoryInfo(category)}
            {@const isExpanded = expandedCategories.has(category)}
            {@const categoryEnabled = isCategoryEnabled(category)}

            {#if categoryTools.length > 0}
              <div class="category" class:disabled={!categoryEnabled}>
                <button
                  class="category-header"
                  onclick={() => toggleCategory(category)}
                >
                  <div class="category-info">
                    <Icon name={isExpanded ? 'chevron-down' : 'chevron-right'} size={16} />
                    <span class="category-name">{info.name}</span>
                    <span class="category-count">({categoryTools.length})</span>
                  </div>
                  <div class="category-actions">
                    {#if category !== 'self_modify'}
                      <button
                        class="category-toggle"
                        class:enabled={categoryEnabled}
                        onclick={(e) => {
                          e.stopPropagation();
                          handleCategoryToggle(category, categoryEnabled);
                        }}
                        title={categoryEnabled ? 'Disable category' : 'Enable category'}
                      >
                        {categoryEnabled ? 'Enabled' : 'Disabled'}
                      </button>
                    {/if}
                  </div>
                </button>

                {#if isExpanded}
                  <div class="category-description">{info.description}</div>
                  <div class="tool-list">
                    {#each categoryTools as tool (tool.name)}
                      {@const badge = getSecurityBadge(tool.securityLevel)}
                      <div class="builtin-tool-item" class:disabled={!tool.enabled}>
                        <div class="tool-info">
                          <div class="tool-header">
                            <span class="tool-name">{tool.name}</span>
                            <span class="security-badge {badge.class}">{badge.text}</span>
                          </div>
                          <p class="tool-description">{tool.description}</p>
                          {#if tool.enabledReason !== 'default'}
                            <span class="enabled-reason">
                              {#if tool.enabledReason === 'user_override'}
                                User override
                              {:else if tool.enabledReason === 'category_disabled'}
                                Category disabled
                              {/if}
                            </span>
                          {/if}
                        </div>
                        <div class="tool-actions">
                          <label class="toggle-switch">
                            <input
                              type="checkbox"
                              checked={tool.enabled}
                              onchange={() => handleBuiltInToggle(tool)}
                              disabled={builtInToolsStore.loading}
                            />
                            <span class="toggle-slider"></span>
                          </label>
                        </div>
                      </div>
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
          {/each}
        </div>
      {/if}
    </div>

  {:else}
    <!-- Custom Tools Section -->
    <div class="section">
      <div class="header">
        <div class="header-actions">
          <Button variant="primary" onclick={() => (showCreateForm = true)}>
            + New Tool
          </Button>
        </div>
      </div>

      <!-- Filters -->
      <div class="filters">
        <div class="search">
          <input
            type="text"
            placeholder="Search tools..."
            bind:value={searchQuery}
          />
        </div>
        <div class="filter-buttons">
          <button class:active={filter === 'all'} onclick={() => (filter = 'all')}>
            All
          </button>
          <button class:active={filter === 'http'} onclick={() => (filter = 'http')}>
            HTTP
          </button>
          <button class:active={filter === 'mcp'} onclick={() => (filter = 'mcp')}>
            MCP
          </button>
        </div>
      </div>

      <!-- Error message -->
      {#if toolsStore.error}
        <div class="error-message">
          <Icon name="error" size={16} />
          {toolsStore.error}
          <button onclick={() => toolsStore.clearError()}>Dismiss</button>
        </div>
      {/if}

      <!-- Loading state -->
      {#if toolsStore.loading}
        <div class="loading">Loading tools...</div>
      {:else if filteredTools().length === 0}
        <div class="empty-state">
          {#if searchQuery || filter !== 'all'}
            <p class="empty-message">No tools match your filters.</p>
            <button class="clear-filters" onclick={() => { searchQuery = ''; filter = 'all'; }}>
              Clear filters
            </button>
          {:else}
            <div class="empty-icon">+</div>
            <h4>No Custom Tools Yet</h4>
            <p class="empty-message">
              Create HTTP or MCP tools to extend your assistant's capabilities.
            </p>
            <Button variant="primary" onclick={() => (showCreateForm = true)}>
              + Create Your First Tool
            </Button>
          {/if}
        </div>
      {:else}
        <!-- Tool list -->
        <div class="tool-list">
          {#each filteredTools() as tool (tool.id)}
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
  {/if}

  <!-- Create form modal -->
  {#if showCreateForm}
    <div class="modal-overlay" onclick={() => (showCreateForm = false)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
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
    <div class="modal-overlay" onclick={() => (editingTool = null)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
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
    <div class="modal-overlay" onclick={() => (testingTool = null)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
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

  <!-- Edit description modal -->
  {#if editingDescriptionTool}
    <div class="modal-overlay" onclick={() => (editingDescriptionTool = null)}>
      <div class="modal modal-sm" onclick={(e) => e.stopPropagation()}>
        <div class="modal-header">
          <h3>Edit Description: {editingDescriptionTool.name}</h3>
          <button class="close-btn" onclick={() => (editingDescriptionTool = null)}>
            &times;
          </button>
        </div>
        <div class="modal-body">
          <p>Customize how this tool is described to the AI. This helps the AI understand when and how to use this tool.</p>

          <div class="form-group">
            <label for="description-input">Description</label>
            <textarea
              id="description-input"
              rows={4}
              bind:value={descriptionInput}
              placeholder="Enter a custom description..."
            ></textarea>
          </div>

          {#if editingDescriptionTool.customDescription}
            <div class="info-box">
              <Icon name="info" size={14} />
              <span>This tool has a custom description. Click "Reset to Default" to restore the original.</span>
            </div>
          {/if}

          <div class="modal-actions">
            <Button variant="ghost" onclick={() => (editingDescriptionTool = null)}>
              Cancel
            </Button>
            {#if editingDescriptionTool.customDescription || descriptionInput !== editingDescriptionTool.defaultDescription}
              <Button variant="ghost" onclick={resetDescription}>
                Reset to Default
              </Button>
            {/if}
            <Button variant="primary" onclick={saveDescription} disabled={unifiedToolsStore.loading}>
              Save Description
            </Button>
          </div>
        </div>
      </div>
    </div>
  {/if}

  <!-- Configure tool modal -->
  {#if editingConfigTool && editingConfigTool.configSchema}
    <div class="modal-overlay" onclick={() => (editingConfigTool = null)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
        <div class="modal-header">
          <h3>Configure: {editingConfigTool.name}</h3>
          <button class="close-btn" onclick={() => (editingConfigTool = null)}>
            &times;
          </button>
        </div>
        <div class="modal-body">
          <p>Configure settings for this tool. Changes will be saved to your profile.</p>

          <div class="config-form">
            {#each Object.entries(editingConfigTool.configSchema) as [key, schema]}
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

          <div class="modal-actions">
            <Button variant="ghost" onclick={() => (editingConfigTool = null)}>
              Cancel
            </Button>
            <Button variant="primary" onclick={saveConfig} disabled={unifiedToolsStore.loading}>
              Save Configuration
            </Button>
          </div>
        </div>
      </div>
    </div>
  {/if}

</div>

<style>
  .tool-management {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  /* Unified view styles */
  .unified-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
  }

  .unified-header h3 {
    margin: 0;
    color: var(--text-primary);
  }

  .unified-filters {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .category-filters {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-xs);
  }

  .category-chip {
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-full);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .category-chip:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .category-chip.active {
    background: var(--accent-primary);
    color: var(--bg-primary);
    border-color: var(--accent-primary);
  }

  .stats-row {
    display: flex;
    gap: var(--spacing-lg);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .stat {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .stat strong {
    color: var(--text-primary);
  }

  .unified-categories {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .unified-tool-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .unified-tool-item:last-child {
    border-bottom: none;
  }

  .unified-tool-item.disabled {
    opacity: 0.6;
  }

  .type-badge {
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
  }

  .badge-system {
    background: rgba(99, 102, 241, 0.15);
    color: rgb(129, 140, 248);
  }

  .badge-http {
    background: rgba(34, 211, 238, 0.15);
    color: var(--accent-primary);
  }

  .badge-mcp {
    background: rgba(168, 85, 247, 0.15);
    color: rgb(192, 132, 252);
  }

  .badge-custom {
    background: rgba(251, 191, 36, 0.15);
    color: var(--warning);
  }

  .tabs {
    display: flex;
    gap: var(--spacing-xs);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-sm);
  }

  .tab {
    padding: var(--spacing-sm) var(--spacing-md);
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    cursor: pointer;
    border-radius: var(--radius-sm) var(--radius-sm) 0 0;
    transition: all 0.15s ease;
  }

  .tab:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .tab.active {
    color: var(--accent-primary);
    border-bottom: 2px solid var(--accent-primary);
    margin-bottom: -1px;
  }

  .section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
  }

  .header-info {
    flex: 1;
  }

  .header-description {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .header h3 {
    margin: 0;
    color: var(--text-primary);
  }

  .filters {
    display: flex;
    gap: var(--spacing-md);
    align-items: center;
  }

  .search {
    flex: 1;
  }

  .search input {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .search input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .filter-buttons {
    display: flex;
    gap: var(--spacing-xs);
  }

  .filter-buttons button {
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .filter-buttons button:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .filter-buttons button.active {
    background: var(--accent-primary);
    color: var(--bg-primary);
    border-color: var(--accent-primary);
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

  .loading {
    text-align: center;
    padding: var(--spacing-xl);
    color: var(--text-muted);
  }

  /* Categories */
  .categories {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .category {
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .category.disabled {
    opacity: 0.6;
  }

  .category-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: var(--spacing-md);
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    color: var(--text-primary);
  }

  .category-header:hover {
    background: var(--bg-hover);
  }

  .category-info {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .category-name {
    font-weight: 500;
  }

  .category-count {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .category-toggle {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all 0.15s ease;
    background: var(--bg-elevated-3);
    border: 1px solid var(--border-subtle);
    color: var(--text-muted);
  }

  .category-toggle.enabled {
    background: rgba(74, 222, 128, 0.15);
    color: var(--success);
    border-color: var(--success);
  }

  .category-description {
    padding: 0 var(--spacing-md) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .category > .tool-list {
    border-top: 1px solid var(--border-subtle);
  }

  /* Built-in tool items */
  .builtin-tool-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .builtin-tool-item:last-child {
    border-bottom: none;
  }

  .builtin-tool-item.disabled {
    opacity: 0.6;
  }

  /* Toggle switch */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--bg-elevated-3);
    border: 1px solid var(--border-subtle);
    border-radius: 24px;
    transition: 0.15s;
  }

  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 2px;
    bottom: 2px;
    background-color: var(--text-muted);
    border-radius: 50%;
    transition: 0.15s;
  }

  .toggle-switch input:checked + .toggle-slider {
    background-color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .toggle-switch input:checked + .toggle-slider:before {
    transform: translateX(20px);
    background-color: white;
  }

  .toggle-switch input:disabled + .toggle-slider {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Security badges */
  .security-badge {
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
  }

  .badge-safe {
    background: rgba(74, 222, 128, 0.15);
    color: var(--success);
  }

  .badge-moderate {
    background: rgba(251, 191, 36, 0.15);
    color: var(--warning);
  }

  .badge-sensitive {
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  .enabled-reason {
    display: inline-block;
    margin-top: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-style: italic;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xl) var(--spacing-md);
    text-align: center;
    min-height: 200px;
  }

  .empty-icon {
    font-size: 48px;
    margin-bottom: var(--spacing-md);
    opacity: 0.6;
    width: 64px;
    height: 64px;
    border: 2px dashed var(--border-subtle);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
  }

  .empty-state h4 {
    margin: 0 0 var(--spacing-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
  }

  .empty-message {
    margin: 0 0 var(--spacing-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    max-width: 300px;
  }

  .clear-filters {
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    cursor: pointer;
    font-size: var(--font-size-sm);
    transition: all 0.15s ease;
  }

  .clear-filters:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

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

  .tool-info {
    flex: 1;
    min-width: 0;
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
  }

  .tool-name {
    font-weight: 500;
    color: var(--text-primary);
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

  .action-btn.edit-desc {
    color: var(--text-secondary);
    border-color: var(--border-default);
  }

  .action-btn.edit-desc:hover {
    background: rgba(139, 92, 246, 0.15);
    color: rgb(167, 139, 250);
    border-color: rgb(139, 92, 246);
  }

  .action-btn.configure {
    color: var(--text-secondary);
    border-color: var(--border-default);
  }

  .action-btn.configure:hover {
    background: rgba(34, 211, 238, 0.15);
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  /* Modal styles */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    width: 90%;
    max-width: 600px;
    max-height: 90vh;
    overflow: auto;
  }

  .modal.modal-sm {
    max-width: 450px;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
  }

  .modal-header h3 {
    margin: 0;
    color: var(--text-primary);
  }

  .modal-body {
    padding: var(--spacing-md);
  }

  .modal-body p {
    margin: var(--spacing-md) 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-lg);
  }

  .warning-box {
    display: flex;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.3);
    border-radius: var(--radius-md);
    color: var(--error);
  }

  .warning-box code {
    background: rgba(0, 0, 0, 0.2);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
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

  /* Custom description badge */
  .custom-badge {
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    background: rgba(139, 92, 246, 0.15);
    color: rgb(167, 139, 250);
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
</style>
