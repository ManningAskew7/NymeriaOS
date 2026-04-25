<script lang="ts">
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';
  import MCPServerPanel from './MCPServerPanel.svelte';
  import { onMount } from 'svelte';

  // Tools whose runtime is gated by require_admin_user on the backend.
  // For non-admin callers the toggle still flips here but the agent will
  // get 403 trying to invoke them — surface that up-front via a badge.
  const ADMIN_ONLY_TOOLS = new Set(['self_modify', 'claude_code']);
  let isAdmin = $derived(configStore.identity?.role === 'admin');
  function isAdminOnlyTool(name: string): boolean {
    return ADMIN_ONLY_TOOLS.has(name);
  }

  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

  let searchQuery = $state('');

  onMount(() => {
    if (!unifiedToolsStore.loaded) {
      unifiedToolsStore.loadTools();
    }
  });

  let filteredCategories = $derived.by(() => {
    const byCategory = unifiedToolsStore.toolsByCategory;
    const query = searchQuery.toLowerCase();

    if (!query) return byCategory;

    const filtered: Record<string, typeof unifiedToolsStore.tools> = {};
    for (const [cat, tools] of Object.entries(byCategory)) {
      const matching = tools.filter(t =>
        t.name.toLowerCase().includes(query) ||
        t.description.toLowerCase().includes(query)
      );
      if (matching.length > 0) filtered[cat] = matching;
    }
    return filtered;
  });

  async function handleToggle(toolId: string, enabled: boolean) {
    await unifiedToolsStore.setToolEnabled(toolId, enabled);
  }
</script>

{#if open}
  <div class="tool-panel">
    <div class="panel-header">
      <button class="back-btn" onclick={onClose}>
        <Icon name="chevronLeft" size={22} />
      </button>
      <h2>Tools</h2>
      <span class="tool-count">
        {unifiedToolsStore.enabledTools.length}/{unifiedToolsStore.tools.length}
      </span>
    </div>

    <div class="search-bar">
      <Icon name="tool" size={16} />
      <input
        type="text"
        placeholder="Search tools..."
        bind:value={searchQuery}
      />
    </div>

    <div class="panel-body">
      {#if unifiedToolsStore.loading && unifiedToolsStore.tools.length === 0}
        <div class="loading-state">
          <Spinner size="md" />
        </div>
      {:else}
        <MCPServerPanel />

        {#each Object.entries(filteredCategories) as [category, tools]}
          {@const info = unifiedToolsStore.getCategoryInfo(category)}
          <div class="category">
            <div class="category-header">
              <span class="category-name">{info.name}</span>
              <span class="category-count">{tools.filter(t => t.enabled).length}/{tools.length}</span>
            </div>
            {#each tools as tool (tool.id)}
              <div class="tool-item">
                <div class="tool-info">
                  <span class="tool-name">
                    {tool.name}
                    {#if isAdminOnlyTool(tool.name)}
                      <span
                        class="admin-only-badge"
                        title={isAdmin
                          ? 'Requires admin role'
                          : "You don't have the admin role — toggling this tool will work but the agent will hit 403 when invoking it"}
                      >
                        admin only
                      </span>
                    {/if}
                  </span>
                  <span class="tool-desc">{tool.description}</span>
                </div>
                <label class="tool-toggle">
                  <input
                    type="checkbox"
                    checked={tool.enabled}
                    onchange={() => handleToggle(tool.id, !tool.enabled)}
                  />
                </label>
              </div>
            {/each}
          </div>
        {/each}
      {/if}
    </div>
  </div>
{/if}

<style>
  .tool-panel {
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

  .tool-count {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .search-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .search-bar input {
    flex: 1;
    border: none;
    background: transparent;
    font-size: 16px;
    color: var(--text-primary);
    padding: var(--spacing-xs) 0;
    min-height: 36px;
  }

  .search-bar input:focus {
    outline: none;
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  .loading-state {
    display: flex;
    justify-content: center;
    padding: var(--spacing-xl);
  }

  .category {
    border-bottom: 1px solid var(--border-subtle);
  }

  .category-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-lg);
    background: var(--bg-elevated);
    position: sticky;
    top: 0;
    z-index: 1;
  }

  .category-name {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .category-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .tool-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-lg);
    min-height: var(--touch-target-min);
  }

  .tool-info {
    flex: 1;
    min-width: 0;
  }

  .tool-name {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .admin-only-badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    padding: 0 5px;
    margin-left: 4px;
    border-radius: var(--radius-sm);
    background: rgba(251, 191, 36, 0.12);
    color: var(--warning, #fbbf24);
    border: 1px solid rgba(251, 191, 36, 0.4);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    vertical-align: middle;
  }

  .tool-desc {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .tool-toggle input {
    width: 20px;
    height: 20px;
  }
</style>
