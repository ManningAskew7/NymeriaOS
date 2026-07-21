<script lang="ts">
  import { onMount } from 'svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import Icon from '../common/Icon.svelte';
  import InlineLoader from '../common/InlineLoader.svelte';
  import MCPServerPanel from './MCPServerPanel.svelte';

  // Commit actions live in the host panel's shared footer (SettingsPanel),
  // wired through these bindables plus the exported save/discardChanges.
  interface Props {
    dirtyCount?: number;
    busy?: boolean;
    statusText?: string;
    statusKind?: 'idle' | 'success' | 'error';
  }

  let {
    dirtyCount = $bindable(0),
    busy = $bindable(false),
    statusText = $bindable(''),
    statusKind = $bindable('idle'),
  }: Props = $props();

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

  // Symmetric difference vs the saved selection: the MCP tab's dirty count.
  const changedToolCount = $derived.by(() => {
    const saved = new Set(defaultToolsStore.defaultToolNames);
    let changed = 0;
    for (const toolName of selectedTools) {
      if (!saved.has(toolName)) changed += 1;
    }
    for (const toolName of saved) {
      if (!selectedTools.has(toolName)) changed += 1;
    }
    return changed;
  });

  $effect(() => {
    dirtyCount = changedToolCount;
    busy = defaultToolsStore.saving;
    statusText = saveMessage;
    statusKind = saveStatus;
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

  // Called by the MCP tab's shared footer Discard (SettingsPanel).
  export function discardChanges() {
    selectedTools = new Set(defaultToolsStore.defaultToolNames);
    saveStatus = 'idle';
    saveMessage = '';
  }

  // Called by the MCP tab's shared footer Save (SettingsPanel).
  export async function save() {
    const ok = await defaultToolsStore.save([...selectedTools]);
    if (ok) {
      selectedTools = new Set(defaultToolsStore.defaultToolNames);
      saveStatus = 'success';
      saveMessage = 'MCP default tools saved!';
    } else {
      saveStatus = 'error';
      saveMessage = defaultToolsStore.error || "Couldn't save your default tools. Try again in a moment.";
    }
    setTimeout(() => { saveMessage = ''; saveStatus = 'idle'; }, 3000);
  }
</script>

<div class="mcp-management">
  <div class="panel-scroll">
    {#if defaultToolsStore.loading}
      <div class="loading"><InlineLoader text="Loading MCP servers…" /></div>
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

  <!-- Save/Discard live in the host panel's shared footer (SettingsPanel). -->
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
    padding: 1rem 1.25rem;
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
    font-size: var(--font-size-sm);
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
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-primary) 25%, transparent);
    border-radius: var(--radius-sm);
    padding: 0.2rem 0.45rem;
  }

  .inline-warning {
    margin-top: 0.75rem;
    padding: 0.65rem 0.8rem;
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-md);
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 8%, transparent);
    font-size: 0.83rem;
  }

  .panel-hint {
    margin: 0.65rem 0 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

</style>
