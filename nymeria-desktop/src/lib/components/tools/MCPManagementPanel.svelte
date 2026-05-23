<script lang="ts">
  import { onMount } from 'svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import Icon from '../common/Icon.svelte';
  import MCPServerPanel from './MCPServerPanel.svelte';

  let selectedTools = $state<Set<string>>(new Set());
  let initialized = $state(false);
  let saveMessage = $state('');
  let saveStatus = $state<'idle' | 'success' | 'error'>('idle');

  onMount(() => {
    mcpServersStore.refresh();
    defaultToolsStore.resetLoaded();
    defaultToolsStore.load();
  });

  $effect(() => {
    if (defaultToolsStore.loaded && !initialized) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      initialized = true;
    }
  });

  const mcpDefaultCount = $derived(
    [...selectedTools].filter((name) => name.startsWith('mcp__')).length
  );

  const totalWithCallable = $derived(selectedTools.size + defaultToolsStore.callableThreadCount);

  const hasChanges = $derived.by(() => {
    const saved = new Set(defaultToolsStore.defaultToolNames);
    if (selectedTools.size !== saved.size) return true;
    for (const toolName of selectedTools) {
      if (!saved.has(toolName)) return true;
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

  function discardChanges() {
    selectedTools = new Set(defaultToolsStore.defaultToolNames);
    saveStatus = 'idle';
    saveMessage = '';
  }

  async function handleSave() {
    const ok = await defaultToolsStore.save([...selectedTools]);
    if (ok) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      saveStatus = 'success';
      saveMessage = 'MCP default tools saved!';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || 'Failed to save';
    }
    setTimeout(() => { saveMessage = ''; saveStatus = 'idle'; }, 3000);
  }
</script>

<div class="mcp-management">
  <div class="panel-scroll">
    {#if defaultToolsStore.loading}
      <div class="loading">Loading MCP servers...</div>
    {:else if !defaultToolsStore.loaded}
      <div class="loading">Connect to the API to configure MCP servers.</div>
    {:else}
      <div class="summary-bar">
        <div class="summary-left">
          <span class="summary-count">
            <strong>{mcpDefaultCount}</strong> MCP tools enabled by default
          </span>
          <span class="mode-badge mcp">
            <Icon name="terminal" size={13} />
            {mcpServersStore.servers.length} servers
          </span>
        </div>
      </div>
      <p class="panel-hint">
        Toggle discovered server tools on to add them to the default core tool set inherited by every new thread.
      </p>

      {#if totalWithCallable > 25}
        <div class="inline-warning">
          {totalWithCallable} tools total (including callable threads). High tool counts can degrade model performance
        </div>
      {/if}

      <MCPServerPanel {selectedTools} onToggleTool={toggleTool} />
    {/if}
  </div>

  {#if defaultToolsStore.loaded}
    <div class="panel-footer panel-footer-pinned">
      <button
        class="btn btn-ghost"
        onclick={discardChanges}
        disabled={defaultToolsStore.saving || !hasChanges}
        type="button"
      >
        Discard Changes
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
</div>

<style>
  .mcp-management {
    height: 100%;
    min-height: 0;
    display: flex;
    flex-direction: column;
    position: relative;
  }

  .panel-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 1rem 1.25rem 5.5rem 1.25rem;
  }

  .loading {
    padding: 2rem;
    text-align: center;
    color: var(--text-muted);
  }

  .summary-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 0.9rem;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  .summary-left {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    flex-wrap: wrap;
  }

  .summary-count {
    font-size: 0.9rem;
    color: var(--text-secondary);
  }

  .summary-count strong {
    color: var(--text-primary);
  }

  .mode-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--font-size-xs);
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 25%, transparent);
    border-radius: var(--radius-sm);
    padding: 0.2rem 0.45rem;
  }

  .inline-warning {
    margin-top: 0.75rem;
    padding: 0.65rem 0.8rem;
    border: 1px solid rgba(245, 158, 11, 0.35);
    border-radius: var(--radius-md);
    color: #f59e0b;
    background: rgba(245, 158, 11, 0.08);
    font-size: 0.83rem;
  }

  .panel-hint {
    margin: 0.65rem 0 0;
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.4;
  }

  .panel-footer {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    justify-content: flex-end;
    gap: 0.65rem;
    padding: 0.75rem 1.25rem;
    /* No border / background — buttons float against whatever sits behind
       the panel so the bar doesn't read as a separate dark strip. */
    background: transparent;
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 34px;
    padding: 0.45rem 0.85rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .btn-ghost {
    color: var(--text-secondary);
    background: transparent;
  }

  .btn-primary {
    color: var(--button-primary-text, #fff);
    background: var(--accent);
    border-color: var(--accent);
  }

  .save-message {
    position: absolute;
    right: 1.25rem;
    bottom: 4rem;
    padding: 0.5rem 0.7rem;
    border-radius: var(--radius-sm);
    font-size: 0.8rem;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
  }

  .save-message.success {
    color: #22c55e;
    border-color: rgba(34, 197, 94, 0.35);
  }

  .save-message.error {
    color: #ef4444;
    border-color: rgba(239, 68, 68, 0.35);
  }
</style>
