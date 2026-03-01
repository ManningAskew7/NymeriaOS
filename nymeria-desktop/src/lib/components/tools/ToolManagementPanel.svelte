<script lang="ts">
  import { toolsStore } from '$lib/stores/tools.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import type { CustomTool, CustomToolCreateRequest, UnifiedTool, DefaultToolInfo } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
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
    memory: { name: 'Memory', icon: 'brain' },
    todo: { name: 'TODOs', icon: 'list' },
    self_modify: { name: 'Self-Modify', icon: 'code' },
    subagent: { name: 'Utilities', icon: 'refresh' },
    trigger: { name: 'Triggers', icon: 'zap' },
    email: { name: 'Outlook Email', icon: 'mail' },
    browser: { name: 'Browser', icon: 'globe' },
    calendar: { name: 'Google Calendar', icon: 'calendar' },
    custom: { name: 'Custom', icon: 'puzzle' },
  };

  const CATEGORY_ORDER = ['core', 'memory', 'todo', 'trigger', 'email', 'browser', 'calendar', 'self_modify', 'subagent', 'custom'];

  // --- Custom tools state ---
  let showCreateForm = $state(false);
  let editingTool = $state<CustomTool | null>(null);
  let testingTool = $state<CustomTool | null>(null);
  let customFilter = $state<'all' | 'http' | 'mcp'>('all');

  // Description/config editing
  let editingDescriptionTool = $state<UnifiedTool | null>(null);
  let editingConfigTool = $state<UnifiedTool | null>(null);
  let descriptionInput = $state('');
  let configInputs = $state<Record<string, unknown>>({});

  // Load stores on mount
  $effect(() => {
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
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
  const filteredTools = $derived(() => {
    if (!searchQuery.trim()) return defaultToolsStore.tools;
    const q = searchQuery.toLowerCase();
    return defaultToolsStore.tools.filter(
      (t: DefaultToolInfo) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q)
    );
  });

  // Split into core (selected) and available (not selected), grouped by category
  const coreToolsByCategory = $derived(() => {
    const result: Record<string, DefaultToolInfo[]> = {};
    for (const tool of filteredTools()) {
      if (selectedTools.has(tool.name)) {
        if (!result[tool.category]) result[tool.category] = [];
        result[tool.category].push(tool);
      }
    }
    return result;
  });

  const availableToolsByCategory = $derived(() => {
    const result: Record<string, DefaultToolInfo[]> = {};
    for (const tool of filteredTools()) {
      if (!selectedTools.has(tool.name)) {
        if (!result[tool.category]) result[tool.category] = [];
        result[tool.category].push(tool);
      }
    }
    return result;
  });

  const coreCount = $derived(selectedTools.size);
  const availableCount = $derived(defaultToolsStore.tools.length - selectedTools.size);
  const totalWithCallable = $derived(selectedTools.size + defaultToolsStore.callableThreadCount);

  const hasChanges = $derived(() => {
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
    const ok = await defaultToolsStore.reset();
    if (ok) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
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
  const filteredCustomTools = $derived(() => {
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

  // Description editing
  function openDescriptionEditor(tool: UnifiedTool) {
    editingDescriptionTool = tool;
    descriptionInput = tool.customDescription || tool.defaultDescription;
  }

  async function saveDescription() {
    if (!editingDescriptionTool) return;
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

  // Config editing
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

  function handleModalKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      if (showCreateForm) { showCreateForm = false; }
      else if (editingTool) { editingTool = null; }
      else if (testingTool) { testingTool = null; }
      else if (editingDescriptionTool) { editingDescriptionTool = null; }
      else if (editingConfigTool) { editingConfigTool = null; }
    }
  }
</script>

<div class="tool-management">
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
        {#if defaultToolsStore.mode === 'custom'}
          <span class="mode-badge custom">Custom</span>
        {:else}
          <span class="mode-badge legacy">Default</span>
        {/if}
      </div>
    </div>

    {#if totalWithCallable > 25}
      <div class="inline-warning">
        {totalWithCallable} tools total (including callable threads) — high tool counts can degrade model performance
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
      <div class="section-group">
        <div class="section-header">
          <span class="section-title">Core Tools</span>
          <span class="section-count">{coreCount}</span>
        </div>
        <p class="section-hint">Loaded automatically in every new thread. Toggle off to move to Available.</p>
        <div class="tools-list">
          {#each CATEGORY_ORDER as category}
            {#if coreToolsByCategory()[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = coreToolsByCategory()[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row selected">
                      <div class="tool-info">
                        <span class="tool-name">{tool.name}</span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <button
                        class="tool-toggle"
                        onclick={() => toggleTool(tool.name)}
                        type="button"
                        title="Remove from core"
                      >
                        <span class="toggle-track">
                          <span class="toggle-thumb"></span>
                        </span>
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}

          <!-- Categories not in CATEGORY_ORDER -->
          {#each Object.keys(coreToolsByCategory()) as category}
            {#if !CATEGORY_ORDER.includes(category) && coreToolsByCategory()[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = coreToolsByCategory()[category]}
              <div class="category-group">
                <div class="category-label">
                  <span class="category-name">{info.name}</span>
                  <span class="category-count">{categoryTools.length}</span>
                </div>
                <div class="category-tools">
                  {#each categoryTools as tool (tool.name)}
                    <div class="tool-row selected">
                      <div class="tool-info">
                        <span class="tool-name">{tool.name}</span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <button
                        class="tool-toggle"
                        onclick={() => toggleTool(tool.name)}
                        type="button"
                        title="Remove from core"
                      >
                        <span class="toggle-track">
                          <span class="toggle-thumb"></span>
                        </span>
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}
        </div>
      </div>
    {/if}

    <!-- Available Tools Section -->
    {#if availableCount > 0}
      <div class="section-group available-section">
        <div class="section-header">
          <span class="section-title">Available Tools</span>
          <span class="section-count">{availableCount}</span>
        </div>
        <p class="section-hint">Not loaded by default. Toggle on to promote to Core, or enable per-thread in thread settings.</p>
        <div class="tools-list">
          {#each CATEGORY_ORDER as category}
            {#if availableToolsByCategory()[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = availableToolsByCategory()[category]}
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
                        </span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <button
                        class="tool-toggle off"
                        onclick={() => toggleTool(tool.name)}
                        type="button"
                        title="Add to core"
                      >
                        <span class="toggle-track">
                          <span class="toggle-thumb"></span>
                        </span>
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}

          <!-- Categories not in CATEGORY_ORDER -->
          {#each Object.keys(availableToolsByCategory()) as category}
            {#if !CATEGORY_ORDER.includes(category) && availableToolsByCategory()[category]?.length}
              {@const info = getCategoryInfo(category)}
              {@const categoryTools = availableToolsByCategory()[category]}
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
                      <button
                        class="tool-toggle off"
                        onclick={() => toggleTool(tool.name)}
                        type="button"
                        title="Add to core"
                      >
                        <span class="toggle-track">
                          <span class="toggle-thumb"></span>
                        </span>
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}
        </div>
      </div>
    {/if}

    <!-- Footer -->
    <div class="panel-footer">
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
        disabled={defaultToolsStore.saving || !hasChanges()}
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
    {:else if filteredCustomTools().length === 0}
      <div class="empty-state">
        <div class="empty-icon">+</div>
        <h4>No Custom Tools Yet</h4>
        <p class="empty-message">
          Create HTTP or MCP tools to extend your assistant's capabilities.
        </p>
      </div>
    {:else}
      <div class="tool-list">
        {#each filteredCustomTools() as tool (tool.id)}
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

  <!-- Edit description modal -->
  {#if editingDescriptionTool}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) editingDescriptionTool = null; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal modal-sm">
        <div class="modal-header">
          <h3>Edit Description: {editingDescriptionTool.name}</h3>
          <button class="close-btn" onclick={() => (editingDescriptionTool = null)}>
            &times;
          </button>
        </div>
        <div class="modal-body">
          <p>Customize how this tool is described to the AI.</p>

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
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) editingConfigTool = null; }} onkeydown={handleModalKeydown} role="dialog" aria-modal="true" tabindex="-1">
      <div class="modal">
        <div class="modal-header">
          <h3>Configure: {editingConfigTool.name}</h3>
          <button class="close-btn" onclick={() => (editingConfigTool = null)}>
            &times;
          </button>
        </div>
        <div class="modal-body">
          <p>Configure settings for this tool.</p>

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

  .mode-badge.legacy {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
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

  /* Section groups (Core / Available) */
  .section-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .section-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-xs);
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
    padding: 0 var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .available-section {
    margin-top: var(--spacing-sm);
  }

  .available-section .section-count {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
  }

  /* Tools list */
  .tools-list {
    max-height: 320px;
    overflow-y: auto;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
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

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Toggle switch */
  .tool-toggle {
    flex-shrink: 0;
    padding: 0;
    background: none;
    border: none;
    cursor: pointer;
  }

  .toggle-track {
    display: block;
    width: 32px;
    height: 18px;
    border-radius: 9px;
    background: var(--accent-primary);
    position: relative;
    transition: background var(--transition-fast);
  }

  .tool-toggle.off .toggle-track {
    background: var(--text-muted);
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 16px;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: white;
    transition: left var(--transition-fast);
  }

  .tool-toggle.off .toggle-thumb {
    left: 2px;
  }

  /* Footer */
  .panel-footer {
    display: flex;
    justify-content: space-between;
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
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
</style>
