<script lang="ts">
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import type { MCPServer, MCPServerCreateRequest } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import MCPServerForm from './MCPServerForm.svelte';
  import MCPInstallModal from './MCPInstallModal.svelte';

  interface Props {
    threadId?: string;
    // When rendered inside ToolManagementPanel, the parent owns the pending
    // selection set and the save button. We update the parent's set via
    // onToggleTool so the Save Changes button lights up and the parent
    // surfaces success / error feedback through its existing save flow.
    selectedTools?: Set<string>;
    onToggleTool?: (name: string) => void;
  }
  let { threadId, selectedTools, onToggleTool }: Props = $props();

  // UI state
  let showInstallModal = $state(false);
  let showAddForm = $state(false);
  let expandedServer = $state<string | null>(null);
  let editingServerId = $state<string | null>(null);
  let confirmDeleteId = $state<string | null>(null);
  let testingServer = $state<string | null>(null);
  let testResults = $state<Record<string, { status: string; error?: string; toolsCount?: number }>>({});
  let discoveringServer = $state<string | null>(null);
  let addLoading = $state(false);
  let addError = $state<string | null>(null);
  let editLoading = $state(false);
  let editError = $state<string | null>(null);

  // Load on mount
  $effect(() => {
    if (!mcpServersStore.loaded && !mcpServersStore.loading) {
      mcpServersStore.load();
    }
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
  });

  // Derive which MCP tool names are currently ticked. Prefer the parent's
  // pending selection (so toggles queue up behind the Save Changes button);
  // fall back to the saved default set if rendered stand-alone.
  let enabledToolNames = $derived(
    selectedTools ?? new Set(defaultToolsStore.defaultToolNames)
  );

  function timeAgo(dateStr: string): string {
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diff = Math.max(0, now - then);
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  function getStatusColor(server: MCPServer): string {
    if (!server.enabled) return 'var(--text-muted, #555)';
    if (server.discoveredTools.length === 0) return '#f59e0b';
    return '#22c55e';
  }

  function getMcpToolName(serverId: string, toolName: string): string {
    return `mcp__${serverId}__${toolName}`;
  }

  async function toggleGlobalTool(serverId: string, toolName: string) {
    const mcpName = getMcpToolName(serverId, toolName);
    if (onToggleTool) {
      // Parent owns the pending set; let it handle reactivity + save button.
      onToggleTool(mcpName);
      return;
    }
    // Standalone fallback: save directly.
    const current = [...defaultToolsStore.defaultToolNames];
    if (current.includes(mcpName)) {
      await defaultToolsStore.save(current.filter(n => n !== mcpName));
    } else {
      await defaultToolsStore.save([...current, mcpName]);
    }
  }

  async function handleAdd(data: MCPServerCreateRequest) {
    addLoading = true;
    addError = null;
    try {
      const result = await mcpServersStore.create(data, threadId);
      if (result.discoveryError) {
        addError = `Server added but discovery failed: ${result.discoveryError}`;
      } else {
        showAddForm = false;
        addError = null;
        // Reload default tools to include newly discovered MCP tools
        defaultToolsStore.resetLoaded();
        await defaultToolsStore.load();
      }
    } catch (e) {
      addError = e instanceof Error ? e.message : 'Failed to add server';
    } finally {
      addLoading = false;
    }
  }

  async function handleEdit(serverId: string, data: MCPServerCreateRequest) {
    editLoading = true;
    editError = null;
    try {
      const result = await mcpServersStore.update(serverId, {
        name: data.name,
        description: data.description,
        server_command: data.server_command,
        server_args: data.server_args,
        env_vars: data.env_vars,
      });
      if (result.discoveryError) {
        editError = `Saved but rediscovery failed: ${result.discoveryError}`;
      } else {
        editingServerId = null;
        editError = null;
        defaultToolsStore.resetLoaded();
        await defaultToolsStore.load();
      }
    } catch (e) {
      editError = e instanceof Error ? e.message : 'Failed to update server';
    } finally {
      editLoading = false;
    }
  }

  async function handleToggleEnabled(server: MCPServer) {
    try {
      await mcpServersStore.update(server.id, { enabled: !server.enabled });
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      console.error('Toggle failed:', e);
    }
  }

  async function handleTest(serverId: string) {
    testingServer = serverId;
    delete testResults[serverId];
    try {
      const result = await mcpServersStore.test(serverId);
      testResults = { ...testResults, [serverId]: result };
      // Auto-dismiss after 8 seconds
      setTimeout(() => {
        if (testResults[serverId]) {
          const { [serverId]: _, ...rest } = testResults;
          testResults = rest;
        }
      }, 8000);
    } catch (e) {
      testResults = { ...testResults, [serverId]: { status: 'error', error: e instanceof Error ? e.message : 'Test failed' } };
    } finally {
      testingServer = null;
    }
  }

  async function handleDiscover(serverId: string) {
    discoveringServer = serverId;
    try {
      await mcpServersStore.discover(serverId);
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      console.error('Discovery failed:', e);
    } finally {
      discoveringServer = null;
    }
  }

  async function handleDelete(serverId: string) {
    try {
      await mcpServersStore.remove(serverId);
      if (expandedServer === serverId) expandedServer = null;
      confirmDeleteId = null;
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      console.error('Delete failed:', e);
    }
  }
</script>

<div class="mcp-panel">
  <div class="panel-header">
    <h4>
      <Icon name="terminal" size={16} />
      MCP Servers
    </h4>
    <div class="header-actions">
      <Button
        size="sm"
        variant="ghost"
        onclick={() => { showAddForm = !showAddForm; addError = null; }}
        title="Manually enter server command, args, and environment variables"
      >
        <Icon name={showAddForm ? 'x' : 'edit'} size={14} />
        {showAddForm ? 'Cancel' : 'Add manually'}
      </Button>
      <Button
        size="sm"
        variant="primary"
        onclick={() => { showInstallModal = true; }}
      >
        <Icon name="bolt" size={14} />
        Install Server
      </Button>
    </div>
  </div>

  <p class="panel-hint">
    Paste a server config, command, URL, or registry ID — Nymeria auto-detects the format, starts the server, and wires its tools into the agent.
  </p>

  <MCPInstallModal
    isOpen={showInstallModal}
    onClose={() => { showInstallModal = false; }}
    {threadId}
  />

  {#if showAddForm}
    <MCPServerForm
      mode="add"
      loading={addLoading}
      error={addError}
      onSubmit={handleAdd}
      onCancel={() => { showAddForm = false; addError = null; }}
    />
  {/if}

  {#if mcpServersStore.loading && mcpServersStore.servers.length === 0}
    <div class="loading-state">Loading MCP servers...</div>
  {:else if mcpServersStore.servers.length === 0 && !showAddForm}
    <div class="empty-state">
      <Icon name="terminal" size={24} />
      <p>No MCP servers yet</p>
      <span>Click <strong>Install Server</strong> above to add one — pasting a command or JSON takes 30 seconds.</span>
    </div>
  {:else}
    <div class="servers-list">
      {#each mcpServersStore.servers as server (server.id)}
        <div class="server-card" class:expanded={expandedServer === server.id} class:disabled-server={!server.enabled}>
          <button
            class="server-header"
            onclick={() => expandedServer = expandedServer === server.id ? null : server.id}
          >
            <span class="status-dot" style="background: {getStatusColor(server)}"></span>
            <div class="server-meta">
              <span class="server-name">{server.name}</span>
              <span class="server-id">{server.id}</span>
            </div>
            <span class="tool-badge">{server.discoveredTools.length} tools</span>
            <span class="updated-at">{timeAgo(server.updatedAt)}</span>
            <label class="enable-toggle" onclick={(e) => e.stopPropagation()}>
              <input
                type="checkbox"
                checked={server.enabled}
                onchange={() => handleToggleEnabled(server)}
              />
              <span class="toggle-track"><span class="toggle-thumb"></span></span>
            </label>
            <Icon name={expandedServer === server.id ? 'chevronDown' : 'chevronRight'} size={16} />
          </button>

          {#if expandedServer === server.id}
            <div class="server-body">
              {#if editingServerId === server.id}
                <MCPServerForm
                  mode="edit"
                  initialData={{
                    id: server.id,
                    name: server.name,
                    description: server.description,
                    server_command: server.serverCommand,
                    server_args: server.serverArgs,
                    env_vars: server.envVars,
                  }}
                  loading={editLoading}
                  error={editError}
                  onSubmit={(data) => handleEdit(server.id, data)}
                  onCancel={() => { editingServerId = null; editError = null; }}
                />
              {:else}
                <div class="server-info-section">
                  <div class="info-row">
                    <span class="info-label">Command</span>
                    <code>{server.serverCommand} {server.serverArgs.join(' ')}</code>
                  </div>
                  {#if server.description}
                    <div class="info-row">
                      <span class="info-label">Description</span>
                      <span>{server.description}</span>
                    </div>
                  {/if}
                </div>

                {#if testResults[server.id]}
                  {@const result = testResults[server.id]}
                  <div class="test-banner" class:test-ok={result.status === 'ok'} class:test-fail={result.status === 'error'}>
                    {#if result.status === 'ok'}
                      <Icon name="success" size={14} />
                      Connected — {result.toolsCount} tools found
                    {:else}
                      <Icon name="error" size={14} />
                      {result.error}
                    {/if}
                  </div>
                {/if}

                {#if server.discoveredTools.length > 0}
                  <div class="tools-section">
                    <span class="tools-heading">Discovered Tools</span>
                    <div class="tool-list">
                      {#each server.discoveredTools as tool}
                        {@const mcpName = getMcpToolName(server.id, tool.name)}
                        {@const isEnabled = enabledToolNames.has(mcpName)}
                        <div
                          class="tool-row"
                          class:tool-enabled={isEnabled}
                          class:tool-row-dormant={!server.enabled}
                          title={!server.enabled ? 'MCP server is not running — enable the server to make this tool available' : ''}
                        >
                          <div class="tool-info">
                            <code class="tool-name-code">{tool.name}</code>
                            {#if tool.description}
                              <span class="tool-description">{tool.description}</span>
                            {/if}
                          </div>
                          <label class="tool-toggle" onclick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={isEnabled}
                              onchange={() => toggleGlobalTool(server.id, tool.name)}
                            />
                            <span class="toggle-track"><span class="toggle-thumb"></span></span>
                          </label>
                        </div>
                      {/each}
                    </div>
                  </div>
                {:else}
                  <div class="no-tools">No tools discovered yet. Try rediscovering.</div>
                {/if}

                <div class="server-actions">
                  <Button size="sm" variant="ghost" onclick={() => handleTest(server.id)} disabled={testingServer === server.id} loading={testingServer === server.id}>
                    Test
                  </Button>
                  <Button size="sm" variant="ghost" onclick={() => handleDiscover(server.id)} disabled={discoveringServer === server.id} loading={discoveringServer === server.id}>
                    Rediscover
                  </Button>
                  <Button size="sm" variant="ghost" onclick={() => { editingServerId = server.id; editError = null; }}>
                    <Icon name="edit" size={14} /> Edit
                  </Button>
                  {#if confirmDeleteId === server.id}
                    <div class="confirm-delete">
                      <span>Delete this server?</span>
                      <Button size="sm" variant="danger" onclick={() => handleDelete(server.id)}>Confirm</Button>
                      <Button size="sm" variant="ghost" onclick={() => confirmDeleteId = null}>Cancel</Button>
                    </div>
                  {:else}
                    <Button size="sm" variant="ghost" onclick={() => confirmDeleteId = server.id}>
                      <Icon name="trash" size={14} /> Delete
                    </Button>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .mcp-panel {
    margin-top: 1rem;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--spacing-sm, 0.5rem);
    margin-bottom: 0.4rem;
  }

  .panel-header h4 {
    margin: 0;
    font-size: 0.95rem;
    color: var(--text-primary, #e0e0e0);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .header-actions {
    display: inline-flex;
    gap: 0.4rem;
    align-items: center;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .panel-hint {
    margin: 0 0 0.75rem 0;
    font-size: 0.78rem;
    color: var(--text-muted, #777);
    line-height: 1.4;
  }

  .loading-state {
    color: var(--text-muted, #777);
    font-size: 0.85rem;
    padding: 1.5rem;
    text-align: center;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.3rem;
    padding: 2rem 1rem;
    color: var(--text-muted, #777);
  }

  .empty-state p {
    margin: 0;
    font-size: 0.9rem;
    color: var(--text-secondary, #999);
  }

  .empty-state span {
    font-size: 0.8rem;
  }

  .servers-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .server-card {
    border: 1px solid var(--border-default, #3a3a3a);
    border-radius: var(--radius-lg, 8px);
    overflow: hidden;
    transition: border-color var(--transition-fast, 0.15s);
  }

  .server-card.expanded {
    border-color: var(--border-hover, #555);
  }

  .server-card.disabled-server {
    opacity: 0.6;
  }

  .server-header {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 0.8rem;
    background: var(--bg-elevated, #2a2a2a);
    border: none;
    cursor: pointer;
    color: var(--text-primary, #e0e0e0);
    font-size: 0.85rem;
    transition: background var(--transition-fast, 0.15s);
  }

  .server-header:hover {
    background: var(--bg-hover, #333);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .server-meta {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    flex: 1;
    min-width: 0;
  }

  .server-name {
    font-weight: 500;
    line-height: 1.2;
  }

  .server-id {
    font-size: 0.72rem;
    color: var(--text-muted, #777);
    font-family: monospace;
  }

  .tool-badge {
    font-size: 0.72rem;
    color: var(--text-secondary, #999);
    background: var(--bg-base, #1a1a1a);
    padding: 0.1rem 0.5rem;
    border-radius: 10px;
    white-space: nowrap;
  }

  .updated-at {
    font-size: 0.7rem;
    color: var(--text-muted, #777);
    white-space: nowrap;
  }

  /* Toggle switch */
  .enable-toggle {
    display: flex;
    align-items: center;
    cursor: pointer;
  }

  .enable-toggle input {
    display: none;
  }

  .toggle-track {
    position: relative;
    width: 32px;
    height: 18px;
    background: var(--bg-base, #1a1a1a);
    border: 1px solid var(--border-default, #3a3a3a);
    border-radius: 9px;
    transition: all var(--transition-fast, 0.15s);
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: var(--text-muted, #777);
    border-radius: 50%;
    transition: all var(--transition-fast, 0.15s);
  }

  .enable-toggle input:checked + .toggle-track {
    background: var(--accent-primary, #6c9fff);
    border-color: var(--accent-primary, #6c9fff);
  }

  .enable-toggle input:checked + .toggle-track .toggle-thumb {
    left: 16px;
    background: white;
  }

  .tool-toggle {
    display: flex;
    align-items: center;
    cursor: pointer;
    flex-shrink: 0;
  }

  .tool-toggle input {
    display: none;
  }

  .tool-toggle .toggle-track {
    width: 28px;
    height: 16px;
  }

  .tool-toggle .toggle-thumb {
    width: 10px;
    height: 10px;
  }

  .tool-toggle input:checked + .toggle-track {
    background: var(--accent-primary, #6c9fff);
    border-color: var(--accent-primary, #6c9fff);
  }

  .tool-toggle input:checked + .toggle-track .toggle-thumb {
    left: 14px;
    background: white;
  }

  /* Server body */
  .server-body {
    padding: 0.8rem;
    background: var(--bg-base, #1a1a1a);
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    border-top: 1px solid var(--border-subtle, #2a2a2a);
  }

  .server-info-section {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .info-row {
    display: flex;
    gap: 0.5rem;
    font-size: 0.82rem;
    align-items: baseline;
  }

  .info-label {
    color: var(--text-muted, #777);
    flex-shrink: 0;
    min-width: 70px;
  }

  .info-row code {
    font-size: 0.78rem;
    background: var(--bg-elevated, #2a2a2a);
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    word-break: break-all;
  }

  .test-banner {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    border-radius: var(--radius-sm, 4px);
    font-size: 0.8rem;
    animation: fadeIn 0.2s ease;
  }

  .test-ok {
    background: rgba(34, 197, 94, 0.12);
    color: #22c55e;
  }

  .test-fail {
    background: rgba(244, 67, 54, 0.12);
    color: #f44336;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .tools-section {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .tools-heading {
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-secondary, #999);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .tool-list {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.5rem;
    border-radius: var(--radius-sm, 4px);
    background: var(--bg-elevated, #2a2a2a);
    transition: background var(--transition-fast, 0.15s);
  }

  .tool-row:hover {
    background: var(--bg-hover, #333);
  }

  .tool-row.tool-enabled {
    border-left: 2px solid var(--accent-primary, #6c9fff);
  }

  .tool-row.tool-row-dormant {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .tool-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .tool-name-code {
    font-size: 0.78rem;
    color: var(--accent-primary, #6c9fff);
  }

  .tool-description {
    font-size: 0.72rem;
    color: var(--text-muted, #777);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .no-tools {
    font-size: 0.82rem;
    color: var(--text-muted, #777);
    padding: 0.5rem;
    text-align: center;
    font-style: italic;
  }

  .server-actions {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    justify-content: flex-end;
    padding-top: 0.25rem;
    border-top: 1px solid var(--border-subtle, #2a2a2a);
  }

  .confirm-delete {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.8rem;
    color: var(--error, #ff6b6b);
    animation: fadeIn 0.2s ease;
  }
</style>
