<script lang="ts">
  import type { Thread, ThreadConfig, ThreadConfigUpdateRequest, UnifiedTool, SubAgent, OptionalTool } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { agentsStore } from '$lib/stores/agents.svelte';
  import { api } from '$lib/services/api.svelte';
  import TriggerConfigTab from '$lib/components/triggers/TriggerConfigTab.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';

  interface Props {
    thread: Thread;
    threadConfig: ThreadConfig | null;
    onClose: () => void;
    onSaved: (config: ThreadConfig) => void;
  }

  let { thread, threadConfig, onClose, onSaved }: Props = $props();

  // Active tab
  let activeTab = $state<'instructions' | 'model' | 'tools' | 'agents' | 'triggers'>('instructions');

  // Form state — initialized from threadConfig
  let instructions = $state(threadConfig?.instructions ?? '');
  let disabledTools = $state<Set<string>>(new Set(threadConfig?.disabledTools ?? []));
  let enabledTools = $state<Set<string>>(new Set(threadConfig?.enabledTools ?? []));

  // Optional tools (fetched from backend)
  let optionalTools = $state<OptionalTool[]>([]);
  let optionalToolsLoading = $state(false);

  // LLM form state
  let llmProvider = $state(threadConfig?.llmConfig?.provider ?? '');
  let llmModel = $state(threadConfig?.llmConfig?.model ?? '');
  let llmTemperature = $state<string>(
    threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature)
      : ''
  );
  let llmMaxTokens = $state<string>(
    threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens)
      : ''
  );
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>(
    threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) as 'true' | 'false'
      : 'default'
  );
  let llmReasoningEffort = $state(threadConfig?.llmConfig?.reasoning_effort ?? '');

  // Search
  let toolSearch = $state('');
  let agentSearch = $state('');
  let saving = $state(false);
  let error = $state('');

  // Ensure tools, agents, and triggers are loaded
  $effect(() => {
    if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) {
      unifiedToolsStore.loadTools();
    }
    if (!agentsStore.loaded && !agentsStore.loading) {
      agentsStore.loadAgents();
    }
    if (!triggersStore.loaded && !triggersStore.loading) {
      triggersStore.loadTriggers();
    }
    if (optionalTools.length === 0 && !optionalToolsLoading) {
      optionalToolsLoading = true;
      api.getOptionalTools().then((tools) => {
        optionalTools = tools;
      }).finally(() => {
        optionalToolsLoading = false;
      });
    }
  });

  // All agent names (from agents store) for filtering
  const agentNames = $derived(new Set(agentsStore.agents.map((a: SubAgent) => a.name)));

  const filteredTools = $derived(() => {
    const allTools = unifiedToolsStore.tools;
    if (!toolSearch.trim()) return allTools;
    const q = toolSearch.toLowerCase();
    return allTools.filter(
      (t: UnifiedTool) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q)
    );
  });

  const filteredAgents = $derived(() => {
    const agents = agentsStore.agents;
    if (!agentSearch.trim()) return agents;
    const q = agentSearch.toLowerCase();
    return agents.filter(
      (a: SubAgent) =>
        a.name.toLowerCase().includes(q) ||
        a.description.toLowerCase().includes(q)
    );
  });

  const disabledToolCount = $derived(
    Array.from(disabledTools).filter((name) => !agentNames.has(name)).length
  );

  const disabledAgentCount = $derived(
    Array.from(disabledTools).filter((name) => agentNames.has(name)).length
  );

  const enabledToolCount = $derived(enabledTools.size);

  function toggleOptionalTool(toolName: string) {
    const next = new Set(enabledTools);
    if (next.has(toolName)) {
      next.delete(toolName);
    } else {
      next.add(toolName);
    }
    enabledTools = next;
  }

  function toggleTool(toolName: string) {
    const next = new Set(disabledTools);
    if (next.has(toolName)) {
      next.delete(toolName);
    } else {
      next.add(toolName);
    }
    disabledTools = next;
  }

  function hasChanges(): boolean {
    const origInstructions = threadConfig?.instructions ?? '';
    const origDisabled = new Set(threadConfig?.disabledTools ?? []);
    const origProvider = threadConfig?.llmConfig?.provider ?? '';
    const origModel = threadConfig?.llmConfig?.model ?? '';
    const origTemp = threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature) : '';
    const origMaxTokens = threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens) : '';
    const origExtThinking = threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) : 'default';
    const origReasoning = threadConfig?.llmConfig?.reasoning_effort ?? '';

    const origEnabled = new Set(threadConfig?.enabledTools ?? []);

    if (instructions !== origInstructions) return true;
    if (disabledTools.size !== origDisabled.size) return true;
    for (const t of disabledTools) {
      if (!origDisabled.has(t)) return true;
    }
    if (enabledTools.size !== origEnabled.size) return true;
    for (const t of enabledTools) {
      if (!origEnabled.has(t)) return true;
    }
    if (llmProvider !== origProvider) return true;
    if (llmModel !== origModel) return true;
    if (llmTemperature !== origTemp) return true;
    if (llmMaxTokens !== origMaxTokens) return true;
    if (llmExtendedThinking !== origExtThinking) return true;
    if (llmReasoningEffort !== origReasoning) return true;

    return false;
  }

  async function handleSave() {
    saving = true;
    error = '';

    try {
      const updates: ThreadConfigUpdateRequest = {};

      // Instructions
      if (instructions.trim()) {
        updates.instructions = instructions.trim();
      } else {
        updates.clear_instructions = true;
      }

      // Disabled tools
      if (disabledTools.size > 0) {
        updates.disabled_tools = Array.from(disabledTools);
      } else {
        updates.clear_disabled_tools = true;
      }

      // Enabled optional tools
      if (enabledTools.size > 0) {
        updates.enabled_tools = Array.from(enabledTools);
      } else {
        updates.clear_enabled_tools = true;
      }

      // LLM config
      const hasLlm = llmProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmExtendedThinking !== 'default' || llmReasoningEffort;

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        if (llmProvider) llm.provider = llmProvider;
        if (llmModel) llm.model = llmModel;
        if (llmTemperature) llm.temperature = parseFloat(llmTemperature);
        if (llmMaxTokens) llm.max_tokens = parseInt(llmMaxTokens, 10);
        if (llmExtendedThinking !== 'default') {
          llm.extended_thinking = llmExtendedThinking === 'true';
        }
        if (llmReasoningEffort) llm.reasoning_effort = llmReasoningEffort;
        updates.llm_config = llm;
      } else {
        updates.clear_llm_config = true;
      }

      const result = await threadConfigStore.updateConfig(thread.id, updates);
      onSaved(result);
      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function handleReset() {
    saving = true;
    error = '';
    try {
      await threadConfigStore.deleteConfig(thread.id);
      // Reset form
      instructions = '';
      disabledTools = new Set();
      enabledTools = new Set();
      llmProvider = '';
      llmModel = '';
      llmTemperature = '';
      llmMaxTokens = '';
      llmExtendedThinking = 'default';
      llmReasoningEffort = '';
      onSaved({
        threadId: thread.id,
        instructions: null,
        disabledTools: [],
        enabledTools: [],
        llmConfig: null,
        createdAt: null,
        updatedAt: null,
        hasCustomizations: false,
      });
      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to reset';
    } finally {
      saving = false;
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('modal-backdrop')) {
      onClose();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="modal-backdrop" onclick={handleBackdropClick}>
  <div class="modal-panel">
    <div class="modal-header">
      <h2>Thread Settings</h2>
      <span class="modal-subtitle">{thread.title}</span>
      <button class="close-btn" onclick={onClose} type="button">
        <Icon name="x" size={18} />
      </button>
    </div>

    <div class="tabs">
      <button
        class="tab"
        class:active={activeTab === 'instructions'}
        onclick={() => (activeTab = 'instructions')}
        type="button"
      >
        Instructions
      </button>
      <button
        class="tab"
        class:active={activeTab === 'model'}
        onclick={() => (activeTab = 'model')}
        type="button"
      >
        Model
      </button>
      <button
        class="tab"
        class:active={activeTab === 'tools'}
        onclick={() => (activeTab = 'tools')}
        type="button"
      >
        Tools
        {#if disabledToolCount > 0}
          <span class="tab-badge">{disabledToolCount}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'agents'}
        onclick={() => (activeTab = 'agents')}
        type="button"
      >
        Agents
        {#if disabledAgentCount > 0}
          <span class="tab-badge">{disabledAgentCount}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'triggers'}
        onclick={() => (activeTab = 'triggers')}
        type="button"
      >
        Triggers
        {#if triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length > 0}
          <span class="tab-badge">{triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length}</span>
        {/if}
      </button>
    </div>

    <div class="tab-content">
      {#if activeTab === 'instructions'}
        <div class="tab-panel">
          <label class="field-label" for="thread-instructions">
            Custom Instructions
          </label>
          <p class="field-hint">
            Appended to the base system prompt for this thread only.
          </p>
          <textarea
            id="thread-instructions"
            class="instructions-input"
            bind:value={instructions}
            placeholder="e.g. Focus on email management. Be concise. Always check the calendar before scheduling."
            maxlength={5000}
            rows={8}
          ></textarea>
          <span class="char-count">{instructions.length} / 5000</span>
        </div>

      {:else if activeTab === 'model'}
        <div class="tab-panel">
          <div class="field-group">
            <label class="field-label" for="llm-provider">Provider</label>
            <select id="llm-provider" class="field-select" bind:value={llmProvider}>
              <option value="">Default (inherit global)</option>
              <option value="openrouter">OpenRouter</option>
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
            </select>
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-model">Model</label>
            <input
              id="llm-model"
              class="field-input"
              type="text"
              bind:value={llmModel}
              placeholder="Leave empty for global default"
            />
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-temp">Temperature</label>
            <input
              id="llm-temp"
              class="field-input"
              type="number"
              min="0"
              max="2"
              step="0.1"
              bind:value={llmTemperature}
              placeholder="Default"
            />
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-max-tokens">Max Output Tokens</label>
            <input
              id="llm-max-tokens"
              class="field-input"
              type="number"
              min="1"
              max="128000"
              step="1"
              bind:value={llmMaxTokens}
              placeholder="Default"
            />
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-ext-thinking">Extended Thinking</label>
            <select id="llm-ext-thinking" class="field-select" bind:value={llmExtendedThinking}>
              <option value="default">Default (inherit global)</option>
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-reasoning">Reasoning Effort</label>
            <select id="llm-reasoning" class="field-select" bind:value={llmReasoningEffort}>
              <option value="">Default (inherit global)</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
        </div>

      {:else if activeTab === 'tools'}
        <div class="tab-panel tools-panel">
          <div class="tools-search">
            <input
              type="text"
              class="field-input"
              bind:value={toolSearch}
              placeholder="Search tools..."
            />
          </div>

          {#if unifiedToolsStore.loading}
            <div class="tools-loading">Loading tools...</div>
          {:else}
            <div class="tools-list">
              {#each filteredTools() as tool (tool.id)}
                <div
                  class="tool-row"
                  class:disabled={disabledTools.has(tool.name)}
                >
                  <div class="tool-info">
                    <span class="tool-name">{tool.name}</span>
                    <span class="tool-desc">{tool.description}</span>
                  </div>
                  <button
                    class="tool-toggle"
                    class:off={disabledTools.has(tool.name)}
                    onclick={() => toggleTool(tool.name)}
                    type="button"
                    title={disabledTools.has(tool.name) ? 'Enable tool' : 'Disable tool'}
                  >
                    <span class="toggle-track">
                      <span class="toggle-thumb"></span>
                    </span>
                  </button>
                </div>
              {/each}
            </div>
          {/if}

          {#if optionalTools.length > 0}
            <div class="optional-tools-section">
              <span class="field-label">
                Optional Tools
                {#if enabledToolCount > 0}
                  <span class="tab-badge">{enabledToolCount}</span>
                {/if}
              </span>
              <p class="field-hint">
                These tools are not loaded by default. Enable them for this thread to give the agent direct access (e.g. Outlook email tools instead of going through OutlookAgent).
              </p>
              <div class="tools-list">
                {#each optionalTools as tool (tool.name)}
                  <div
                    class="tool-row"
                    class:optional-enabled={enabledTools.has(tool.name)}
                  >
                    <div class="tool-info">
                      <span class="tool-name">{tool.name}</span>
                      <span class="tool-desc">{tool.description}</span>
                    </div>
                    <button
                      class="tool-toggle"
                      class:off={!enabledTools.has(tool.name)}
                      onclick={() => toggleOptionalTool(tool.name)}
                      type="button"
                      title={enabledTools.has(tool.name) ? 'Disable optional tool' : 'Enable optional tool'}
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
        </div>

      {:else if activeTab === 'agents'}
        <div class="tab-panel tools-panel">
          <p class="field-hint" style="margin-top: 0;">
            Disable sub-agents to prevent this thread from delegating tasks to them.
          </p>
          <div class="tools-search">
            <input
              type="text"
              class="field-input"
              bind:value={agentSearch}
              placeholder="Search agents..."
            />
          </div>

          {#if agentsStore.loading}
            <div class="tools-loading">Loading agents...</div>
          {:else if filteredAgents().length === 0}
            <div class="tools-loading">No sub-agents available</div>
          {:else}
            <div class="tools-list">
              {#each filteredAgents() as agent (agent.name)}
                <div
                  class="tool-row"
                  class:disabled={disabledTools.has(agent.name)}
                >
                  <div class="tool-info">
                    <span class="tool-name">{agent.name}</span>
                    <span class="tool-desc">{agent.description}</span>
                  </div>
                  <button
                    class="tool-toggle"
                    class:off={disabledTools.has(agent.name)}
                    onclick={() => toggleTool(agent.name)}
                    type="button"
                    title={disabledTools.has(agent.name) ? 'Enable agent' : 'Disable agent'}
                  >
                    <span class="toggle-track">
                      <span class="toggle-thumb"></span>
                    </span>
                  </button>
                </div>
              {/each}
            </div>
          {/if}
        </div>

      {:else if activeTab === 'triggers'}
        <div class="tab-panel">
          <TriggerConfigTab {thread} />
        </div>
      {/if}
    </div>

    {#if error}
      <div class="error-bar">{error}</div>
    {/if}

    <div class="modal-footer">
      <button class="btn btn-ghost" onclick={handleReset} disabled={saving} type="button">
        Reset to Defaults
      </button>
      <div class="footer-right">
        <button class="btn btn-ghost" onclick={onClose} disabled={saving} type="button">
          Cancel
        </button>
        <button
          class="btn btn-primary"
          onclick={handleSave}
          disabled={saving || !hasChanges()}
          type="button"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
    </div>
  </div>
</div>

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-panel {
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    width: min(560px, 90vw);
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-default);
  }

  .modal-header h2 {
    margin: 0;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .modal-subtitle {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    flex-shrink: 0;
    margin-left: auto;
  }

  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid var(--border-default);
    padding: 0 var(--spacing-lg);
  }

  .tab {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all var(--transition-fast);
  }

  .tab:hover {
    color: var(--text-primary);
  }

  .tab.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  .tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--bg-base);
    border-radius: var(--radius-full);
  }

  .tab-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  .tab-panel {
    padding: var(--spacing-lg);
  }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 4px;
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0 0 var(--spacing-sm) 0;
  }

  .field-group {
    margin-bottom: var(--spacing-md);
  }

  .field-input,
  .field-select {
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

  .field-input:focus,
  .field-select:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .field-input::placeholder {
    color: var(--text-muted);
  }

  .field-select {
    cursor: pointer;
  }

  .instructions-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-family: inherit;
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    resize: vertical;
    min-height: 120px;
    transition: border-color var(--transition-fast);
  }

  .instructions-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .instructions-input::placeholder {
    color: var(--text-muted);
  }

  .char-count {
    display: block;
    text-align: right;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: 4px;
  }

  /* Tools / Agents tab */
  .tools-panel {
    padding-bottom: var(--spacing-md);
  }

  .tools-search {
    padding: 0 0 var(--spacing-sm) 0;
  }

  .tools-list {
    max-height: 340px;
    overflow-y: auto;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
  }

  .tools-loading {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle, var(--border-default));
    transition: opacity var(--transition-fast);
  }

  .tool-row:last-child {
    border-bottom: none;
  }

  .tool-row.disabled {
    opacity: 0.5;
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
  }

  .tool-row.disabled .tool-name {
    text-decoration: line-through;
  }

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .optional-tools-section {
    margin-top: var(--spacing-lg);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-default);
  }

  .optional-tools-section > .field-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .tool-row.optional-enabled {
    background: color-mix(in srgb, var(--accent-primary) 5%, transparent);
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
  .modal-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-top: 1px solid var(--border-default);
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-sm);
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

  .error-bar {
    padding: var(--spacing-sm) var(--spacing-lg);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
  }
</style>
