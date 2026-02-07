<script lang="ts">
  import { agentsStore } from '$lib/stores/agents.svelte';
  import { builtInToolsStore } from '$lib/stores/builtInTools.svelte';
  import type { SubAgent, SubAgentCreateRequest, BuiltInTool } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import AgentForm from './AgentForm.svelte';
  import AgentTestPanel from './AgentTestPanel.svelte';

  // UI State
  let showCreateForm = $state(false);
  let editingAgent = $state<SubAgent | null>(null);
  let testingAgent = $state<SubAgent | null>(null);
  let searchQuery = $state('');
  let showBuiltInTools = $state(true);

  // Load agents and built-in tools on mount
  $effect(() => {
    if (!agentsStore.loaded && !agentsStore.loading) {
      agentsStore.loadAgents();
    }
    if (!builtInToolsStore.loaded && !builtInToolsStore.loading) {
      builtInToolsStore.loadTools();
    }
  });

  // Get subagent category tools
  const subagentTools = $derived(() => {
    return builtInToolsStore.byCategory?.subagent || [];
  });

  // Filtered agents
  const filteredAgents = $derived(() => {
    let result = agentsStore.agents;

    // Filter by search
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(
        (a) =>
          a.name.toLowerCase().includes(q) ||
          a.description.toLowerCase().includes(q)
      );
    }

    return result;
  });

  async function handleCreate(request: SubAgentCreateRequest) {
    const agent = await agentsStore.createAgent(request);
    if (agent) {
      showCreateForm = false;
    }
  }

  async function handleUpdate(agentName: string, request: Partial<SubAgentCreateRequest>) {
    const agent = await agentsStore.updateAgent(agentName, request);
    if (agent) {
      editingAgent = null;
    }
  }

  async function handleDelete(agent: SubAgent) {
    if (confirm(`Are you sure you want to delete "${agent.name}"?`)) {
      await agentsStore.deleteAgent(agent.name);
    }
  }

  async function handleToggleEnabled(agent: SubAgent) {
    await agentsStore.toggleAgentEnabled(agent.name);
  }

  async function handleBuiltInToggle(tool: BuiltInTool) {
    await builtInToolsStore.setToolEnabled(tool.name, !tool.enabled);
  }
</script>

<div class="agent-management">
  <div class="header">
    <h3>Sub-Agents</h3>
    <div class="header-actions">
      <Button variant="primary" onclick={() => (showCreateForm = true)}>
        + New Agent
      </Button>
    </div>
  </div>

  <!-- Search -->
  <div class="filters">
    <div class="search">
      <input
        type="text"
        placeholder="Search agents..."
        bind:value={searchQuery}
      />
    </div>
  </div>

  <!-- Built-in Sub-Agent Tools -->
  {#if subagentTools().length > 0}
    <div class="builtin-tools-section">
      <button class="section-header" onclick={() => showBuiltInTools = !showBuiltInTools}>
        <div class="section-title">
          <Icon name="terminal" size={16} />
          <span>Sub-Agent Tools</span>
          <span class="tool-count">{subagentTools().length}</span>
        </div>
        <Icon name={showBuiltInTools ? 'chevron-up' : 'chevron-down'} size={16} />
      </button>
      {#if showBuiltInTools}
        <div class="builtin-tools-list">
          {#each subagentTools() as tool (tool.name)}
            <div class="builtin-tool-item" class:disabled={!tool.enabled}>
              <div class="tool-info">
                <span class="tool-name">{tool.name}</span>
                <p class="tool-description">{tool.description}</p>
              </div>
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
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Error message -->
  {#if agentsStore.error}
    <div class="error-message">
      <Icon name="error" size={16} />
      {agentsStore.error}
      <button onclick={() => agentsStore.clearError()}>Dismiss</button>
    </div>
  {/if}

  <!-- Loading state -->
  {#if agentsStore.loading}
    <div class="loading">Loading agents...</div>
  {:else if filteredAgents().length === 0}
    <div class="empty-state">
      {#if searchQuery}
        <p class="empty-message">No agents match your search.</p>
        <button class="clear-filters" onclick={() => { searchQuery = ''; }}>
          Clear search
        </button>
      {:else}
        <div class="empty-icon">🤖</div>
        <h4>No Sub-Agents Yet</h4>
        <p class="empty-message">
          Create specialized agents to handle specific tasks like email, calendar, or code review.
        </p>
        <Button variant="primary" onclick={() => (showCreateForm = true)}>
          + Create Your First Agent
        </Button>
      {/if}
    </div>
  {:else}
    <!-- Agent list -->
    <div class="agent-list">
      {#each filteredAgents() as agent (agent.name)}
        <div class="agent-item" class:disabled={!agent.enabled}>
          <div class="agent-info">
            <div class="agent-header">
              <span class="agent-name">{agent.name}</span>
              <span class="agent-tools">{agent.tools.length} tools</span>
            </div>
            <p class="agent-description">{agent.description}</p>
            <div class="agent-meta">
              <span class="agent-context">Context: {agent.contextTurns} turns</span>
            </div>
          </div>
          <div class="agent-actions">
            <button
              class="action-btn"
              title="Test agent"
              onclick={() => (testingAgent = agent)}
            >
              <Icon name="play" size={16} />
            </button>
            <button
              class="action-btn"
              title="Edit agent"
              onclick={() => (editingAgent = agent)}
            >
              <Icon name="edit" size={16} />
            </button>
            <button
              class="action-btn"
              class:enabled={agent.enabled}
              title={agent.enabled ? 'Disable' : 'Enable'}
              onclick={() => handleToggleEnabled(agent)}
            >
              <Icon name={agent.enabled ? 'visible' : 'hidden'} size={16} />
            </button>
            <button
              class="action-btn delete"
              title="Delete agent"
              onclick={() => handleDelete(agent)}
            >
              <Icon name="trash" size={16} />
            </button>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Create form modal -->
  {#if showCreateForm}
    <div class="modal-overlay" onclick={() => (showCreateForm = false)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
        <div class="modal-header">
          <h3>Create Sub-Agent</h3>
          <button class="close-btn" onclick={() => (showCreateForm = false)}>
            &times;
          </button>
        </div>
        <AgentForm
          onSubmit={handleCreate}
          onCancel={() => (showCreateForm = false)}
        />
      </div>
    </div>
  {/if}

  <!-- Edit form modal -->
  {#if editingAgent}
    <div class="modal-overlay" onclick={() => (editingAgent = null)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
        <div class="modal-header">
          <h3>Edit Agent: {editingAgent.name}</h3>
          <button class="close-btn" onclick={() => (editingAgent = null)}>
            &times;
          </button>
        </div>
        <AgentForm
          agent={editingAgent}
          onSubmit={(req) => handleUpdate(editingAgent!.name, req)}
          onCancel={() => (editingAgent = null)}
        />
      </div>
    </div>
  {/if}

  <!-- Test panel modal -->
  {#if testingAgent}
    <div class="modal-overlay" onclick={() => (testingAgent = null)}>
      <div class="modal" onclick={(e) => e.stopPropagation()}>
        <div class="modal-header">
          <h3>Test Agent: {testingAgent.name}</h3>
          <button class="close-btn" onclick={() => (testingAgent = null)}>
            &times;
          </button>
        </div>
        <AgentTestPanel
          agent={testingAgent}
          onClose={() => (testingAgent = null)}
        />
      </div>
    </div>
  {/if}
</div>

<style>
  .agent-management {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
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

  .agent-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .agent-item {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: all 0.15s ease;
  }

  .agent-item:hover {
    border-color: var(--border-default);
  }

  .agent-item.disabled {
    opacity: 0.6;
  }

  .agent-info {
    flex: 1;
    min-width: 0;
  }

  .agent-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
  }

  .agent-name {
    font-weight: 500;
    color: var(--text-primary);
  }

  .agent-tools {
    padding: 2px 6px;
    background: var(--bg-elevated-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .agent-description {
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

  .agent-meta {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .agent-actions {
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
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .action-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--border-default);
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

  /* Built-in tools section */
  .builtin-tools-section {
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-3);
    border: none;
    cursor: pointer;
    color: var(--text-primary);
  }

  .section-header:hover {
    background: var(--bg-hover);
  }

  .section-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-weight: 500;
  }

  .tool-count {
    padding: 2px 6px;
    background: var(--bg-elevated);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-weight: normal;
  }

  .builtin-tools-list {
    display: flex;
    flex-direction: column;
    gap: 1px;
    background: var(--border-subtle);
  }

  .builtin-tool-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
  }

  .builtin-tool-item.disabled {
    opacity: 0.6;
  }

  .tool-info {
    flex: 1;
    min-width: 0;
  }

  .tool-name {
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .tool-description {
    margin: 2px 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.3;
  }

  /* Toggle switch */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 40px;
    height: 22px;
    flex-shrink: 0;
    margin-left: var(--spacing-md);
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
    transition: 0.2s;
    border-radius: 22px;
  }

  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 16px;
    width: 16px;
    left: 2px;
    bottom: 2px;
    background-color: var(--text-muted);
    transition: 0.2s;
    border-radius: 50%;
  }

  .toggle-switch input:checked + .toggle-slider {
    background-color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .toggle-switch input:checked + .toggle-slider:before {
    transform: translateX(18px);
    background-color: white;
  }

  .toggle-switch input:disabled + .toggle-slider {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
