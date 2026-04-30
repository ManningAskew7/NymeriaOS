<script lang="ts">
  import { onMount } from 'svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import MCPServerPanel from './MCPServerPanel.svelte';

  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

  const enabledMcpToolCount = $derived(
    defaultToolsStore.defaultToolNames.filter((name) => name.startsWith('mcp__')).length
  );

  onMount(() => {
    if (!mcpServersStore.loaded && !mcpServersStore.loading) {
      mcpServersStore.load();
    }
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
  });
</script>

{#if open}
  <div class="mcp-panel-screen">
    <div class="panel-header">
      <button class="back-btn" onclick={onClose}>
        <Icon name="chevronLeft" size={22} />
      </button>
      <h2>MCP</h2>
      <span class="mcp-count">
        {enabledMcpToolCount}/{mcpServersStore.servers.reduce((total, server) => total + server.discoveredTools.length, 0)}
      </span>
    </div>

    <div class="panel-body">
      <p class="panel-hint">
        Toggle discovered server tools on to add them to the default core tool set inherited by every new thread.
      </p>
      <MCPServerPanel />
    </div>
  </div>
{/if}

<style>
  .mcp-panel-screen {
    position: fixed;
    inset: 0;
    z-index: 300;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-top: env(safe-area-inset-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .panel-header h2 {
    flex: 1;
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  .mcp-count {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: var(--spacing-sm) var(--spacing-md) calc(var(--spacing-xl) + env(safe-area-inset-bottom));
  }

  .panel-hint {
    margin: 0.25rem 0 0.75rem;
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }
</style>
