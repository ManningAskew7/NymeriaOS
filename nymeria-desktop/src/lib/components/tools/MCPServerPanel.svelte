<script lang="ts">
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import type { MCPServer, MCPServerCreateRequest } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import ToggleSwitch from '../common/ToggleSwitch.svelte';
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

  // POST /mcp-servers/install (and the manual create form) are gated by
  // require_admin_user. Hide the buttons for non-admins so the UI doesn't
  // promise actions that will 403.
  let isAdmin = $derived(configStore.identity?.role === 'admin');

  // UI state
  let showInstallModal = $state(false);
  let showAddForm = $state(false);
  let expandedServer = $state<string | null>(null);
  let editingServerId = $state<string | null>(null);
  let confirmDeleteId = $state<string | null>(null);
  let testingServer = $state<string | null>(null);
  let testResults = $state<Record<string, { status: string; error?: string; toolsCount?: number }>>({});
  let discoveringServer = $state<string | null>(null);
  let retryingServer = $state<string | null>(null);
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
    if (server.installStatus === 'failed') return 'var(--error)';
    if (server.installStatus === 'draft' || server.installStatus === 'needs_config') return 'var(--warning)';
    if (server.installStatus === 'disabled') return 'var(--text-muted)';
    if (!server.enabled) return 'var(--text-muted)';
    if (server.discoveredTools.length === 0) return 'var(--warning)';
    return 'var(--success)';
  }

  function getStatusLabel(server: MCPServer): string {
    if (server.installStatus === 'failed') return 'failed';
    if (server.installStatus === 'needs_config') return 'needs config';
    if (server.installStatus === 'disabled') return 'disabled';
    if (server.installStatus === 'draft') return 'draft';
    if (!server.enabled) return 'disabled';
    if (server.discoveredTools.length === 0) return 'no tools';
    return 'ready';
  }

  function getStatusClass(server: MCPServer): string {
    if (server.installStatus === 'failed') return 'status-failed';
    if (server.installStatus === 'draft' || server.installStatus === 'needs_config') return 'status-draft';
    if (!server.enabled) return 'status-disabled';
    if (server.discoveredTools.length === 0) return 'status-draft';
    return 'status-ready';
  }

  function getMcpToolName(serverId: string, toolName: string): string {
    return `mcp__${serverId}__${toolName}`;
  }

  function getServerSubtitle(server: MCPServer): string {
    if (server.sourceType || server.runtimeType) {
      return [server.sourceType || 'manual', server.runtimeType || server.transport].join(' / ');
    }
    return server.transport === 'http' ? 'HTTP endpoint' : 'Local stdio server';
  }

  function toggleExpanded(serverId: string) {
    expandedServer = expandedServer === serverId ? null : serverId;
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
      addError = humanizeErrorText(e, { action: 'create', resource: 'the server' });
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
      editError = humanizeErrorText(e, { action: 'update', resource: 'the server' });
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
      testResults = { ...testResults, [server.id]: { status: 'error', error: humanizeErrorText(e, { action: server.enabled ? 'disable' : 'enable', resource: 'the server' }) } };
      // The toggle sits in the collapsed header; expand the card so the
      // failure banner is visible next to the switch the user just flipped.
      expandedServer = server.id;
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
      testResults = { ...testResults, [serverId]: { status: 'error', error: humanizeErrorText(e, { action: 'test', resource: 'the server' }) } };
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
      testResults = { ...testResults, [serverId]: { status: 'error', error: humanizeErrorText(e, { action: 'load', resource: "the server's tools" }) } };
    } finally {
      discoveringServer = null;
    }
  }

  async function handleRetry(server: MCPServer) {
    retryingServer = server.id;
    delete testResults[server.id];
    try {
      const result = await mcpServersStore.retry(server.id, {
        confirmed: true,
        confirmed_risk_ids: (server.riskSignals || [])
          .filter((signal) => signal.requires_confirmation)
          .map((signal) => signal.id),
      });
      if (result.status === 'draft') {
        testResults = {
          ...testResults,
          [server.id]: {
            status: 'error',
            error: result.discoveryError || result.server.lastError || 'Retry saved another draft',
            toolsCount: result.discoveredTools,
          },
        };
      } else {
        testResults = {
          ...testResults,
          [server.id]: {
            status: 'ok',
            toolsCount: result.discoveredTools,
          },
        };
      }
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      testResults = {
        ...testResults,
        [server.id]: {
          status: 'error',
          error: humanizeErrorText(e, { action: 'load', resource: "the server's tools" }),
        },
      };
    } finally {
      retryingServer = null;
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
      testResults = { ...testResults, [serverId]: { status: 'error', error: humanizeErrorText(e, { action: 'delete', resource: 'the server' }) } };
    }
  }
</script>

<div class="mcp-panel">
  <div class="panel-header">
    <h3>
      <Icon name="terminal" size={16} />
      MCP Servers
    </h3>
    <div class="header-actions">
      {#if isAdmin}
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
      {:else}
        <span class="admin-only-hint" title="Admin role required">
          <Icon name="info" size={14} />
          Admin only
        </span>
      {/if}
    </div>
  </div>

  <p class="panel-hint">
    Paste a server config, command, URL, or registry ID. Nymeria auto-detects the format, starts the server, and wires its tools into the agent.
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
    <div class="loading-state">Loading MCP servers…</div>
  {:else if mcpServersStore.servers.length === 0 && !showAddForm}
    <div class="empty-state">
      <Icon name="terminal" size={24} />
      <p>No MCP servers yet</p>
      <span>Click <strong>Install Server</strong> above to add one. Pasting a command or JSON takes 30 seconds.</span>
    </div>
  {:else}
    <div class="servers-list">
      {#each mcpServersStore.servers as server (server.id)}
        <div class="server-card" class:expanded={expandedServer === server.id} class:disabled-server={!server.enabled}>
          <div class="server-header">
            <button
              type="button"
              class="server-summary"
              onclick={() => toggleExpanded(server.id)}
            >
              <span class="status-dot" style="background: {getStatusColor(server)}"></span>
              <div class="server-meta">
                <span class="server-name">{server.name}</span>
                <span class="server-subtitle">{getServerSubtitle(server)}</span>
              </div>
              <span class="tool-badge">{server.discoveredTools.length} tools</span>
              <span class="install-status {getStatusClass(server)}">{getStatusLabel(server)}</span>
              <span class="updated-at">{timeAgo(server.updatedAt)}</span>
              <span class="expand-chevron" class:rotated={expandedServer === server.id}>
                <Icon name="chevronRight" size={16} />
              </span>
            </button>
            <ToggleSwitch
              checked={server.enabled}
              onclick={() => handleToggleEnabled(server)}
              title={server.enabled ? 'Disable MCP server' : 'Enable MCP server'}
              ariaLabel={`${server.enabled ? 'Disable' : 'Enable'} MCP server ${server.name}`}
              variant="outlined"
            />
          </div>

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
                    <span class="info-label">Runtime ID</span>
                    <code>{server.id}</code>
                  </div>
                  <div class="info-row">
                    <span class="info-label">{server.transport === 'http' ? 'URL' : 'Command'}</span>
                    <code>
                      {#if server.transport === 'http'}
                        {server.url}
                      {:else if server.serverCommand}
                        {server.serverCommand} {server.serverArgs.join(' ')}
                      {:else}
                        Prepared during install
                      {/if}
                    </code>
                  </div>
                  {#if server.sourceType || server.runtimeType}
                    <div class="info-row">
                      <span class="info-label">Runtime</span>
                      <span>{server.sourceType || 'manual'} / {server.runtimeType || server.transport}</span>
                    </div>
                  {/if}
                  {#if server.description}
                    <div class="info-row">
                      <span class="info-label">Description</span>
                      <span>{server.description}</span>
                    </div>
                  {/if}
                </div>

                {#if server.lastError}
                  <div class="test-banner test-fail">
                    <Icon name="error" size={14} />
                    {server.lastError}
                  </div>
                {/if}

                {#if server.missingConfig.length > 0}
                  <div class="missing-config">
                    <Icon name="warning" size={14} />
                    <span>Missing {server.missingConfig.map(field => field.label || field.name).join(', ')}</span>
                  </div>
                {/if}

                {#if testResults[server.id]}
                  {@const result = testResults[server.id]}
                  <div class="test-banner" class:test-ok={result.status === 'ok'} class:test-fail={result.status === 'error'}>
                    {#if result.status === 'ok'}
                      <Icon name="success" size={14} />
                      Connected. {result.toolsCount} tools found
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
                          title={!server.enabled ? 'MCP server is not running. Enable the server to make this tool available' : ''}
                        >
                          <div class="tool-info">
                            <span class="tool-display-name" title={mcpName}>{tool.name}</span>
                            {#if tool.description}
                              <span class="tool-description">{tool.description}</span>
                            {/if}
                          </div>
                          <ToggleSwitch
                            checked={isEnabled}
                            onclick={() => toggleGlobalTool(server.id, tool.name)}
                            title={isEnabled ? 'Disable MCP tool globally' : 'Enable MCP tool globally'}
                            ariaLabel={`${isEnabled ? 'Disable' : 'Enable'} MCP tool ${tool.name} globally`}
                            size="sm"
                            variant="outlined"
                          />
                        </div>
                      {/each}
                    </div>
                  </div>
                {:else}
                  <div class="no-tools">No tools discovered yet. Try rediscovering.</div>
                {/if}

                <div class="server-actions">
                  {#if server.installStatus !== 'ready' || server.lastError}
                    <Button size="sm" variant="secondary" onclick={() => handleRetry(server)} disabled={retryingServer === server.id} loading={retryingServer === server.id}>
                      <Icon name="refresh" size={14} /> Retry
                    </Button>
                  {/if}
                  <Button size="sm" variant="ghost" onclick={() => handleTest(server.id)} disabled={testingServer === server.id || server.installStatus !== 'ready'} loading={testingServer === server.id}>
                    Test
                  </Button>
                  <Button size="sm" variant="ghost" onclick={() => handleDiscover(server.id)} disabled={discoveringServer === server.id || server.installStatus !== 'ready'} loading={discoveringServer === server.id}>
                    Rediscover
                  </Button>
                  <Button size="sm" variant="ghost" onclick={() => { editingServerId = server.id; editError = null; }}>
                    <Icon name="edit" size={14} /> Edit server
                  </Button>
                  {#if confirmDeleteId === server.id}
                    <div class="confirm-delete">
                      <span>Delete this server?</span>
                      <Button size="sm" variant="danger" onclick={() => handleDelete(server.id)}>Delete server</Button>
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
    gap: var(--spacing-sm);
    margin-bottom: 0.4rem;
  }

  .panel-header h3 {
    margin: 0;
    font-size: var(--font-size-base);
    color: var(--text-primary);
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

  .admin-only-hint {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    padding: 4px 8px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .panel-hint {
    margin: 0 0 0.75rem 0;
    font-size: 0.78rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .loading-state {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    padding: 1.5rem;
    text-align: center;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.3rem;
    padding: 2rem 1rem;
    color: var(--text-muted);
  }

  .empty-state p {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .empty-state span {
    font-size: var(--font-size-xs);
  }

  .servers-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .server-card {
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }

  .server-card.expanded {
    border-color: var(--border-default);
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
    background: var(--bg-elevated);
    border: none;
    cursor: pointer;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    transition: background var(--transition-fast);
  }

  .server-header:hover {
    background: var(--bg-hover);
  }

  .server-summary {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0;
    border: none;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .server-summary:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
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

  .server-subtitle {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .tool-badge {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    background: var(--bg-base);
    padding: 0.1rem 0.5rem;
    border-radius: 10px;
    white-space: nowrap;
  }

  .install-status {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.12rem 0.45rem;
    border-radius: 10px;
    border: 1px solid var(--border-subtle);
    white-space: nowrap;
  }

  .status-ready {
    color: var(--success);
    background: color-mix(in srgb, var(--success) 12%, transparent);
    border-color: color-mix(in srgb, var(--success) 25%, transparent);
  }

  .status-draft {
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    border-color: color-mix(in srgb, var(--warning) 25%, transparent);
  }

  .status-failed {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 12%, transparent);
    border-color: color-mix(in srgb, var(--error) 25%, transparent);
  }

  .status-disabled {
    color: var(--text-muted);
    background: var(--bg-base);
  }

  .updated-at {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    white-space: nowrap;
  }

  /* Server body */
  .server-body {
    padding: 0.8rem;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    border-top: 1px solid var(--border-subtle);
  }

  .server-info-section {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .info-row {
    display: flex;
    gap: 0.5rem;
    font-size: var(--font-size-sm);
    align-items: baseline;
  }

  .info-label {
    color: var(--text-muted);
    flex-shrink: 0;
    min-width: 70px;
  }

  .info-row code {
    font-size: 0.78rem;
    background: var(--bg-elevated);
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    word-break: break-all;
  }

  .test-banner {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    animation: dropIn var(--transition-normal);
  }

  .test-ok {
    background: color-mix(in srgb, var(--success) 12%, transparent);
    color: var(--success);
  }

  .test-fail {
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
  }

  .missing-config {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    color: var(--warning);
    font-size: var(--font-size-xs);
  }

  /* Intentional variant of the global fadeIn: drops in 4px from above. */
  @keyframes dropIn {
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
    color: var(--text-secondary);
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
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    transition: background var(--transition-fast);
  }

  .tool-row:hover {
    background: var(--bg-hover);
  }

  .tool-row.tool-enabled {
    border-left: 2px solid var(--accent-primary);
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

  .tool-display-name {
    font-size: 0.78rem;
    color: var(--accent-primary);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tool-description {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .no-tools {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
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
    border-top: 1px solid var(--border-subtle);
  }

  .confirm-delete {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: var(--font-size-xs);
    color: var(--error);
    animation: dropIn var(--transition-normal);
  }

  .expand-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
  }

  .expand-chevron.rotated {
    transform: rotate(90deg);
  }
</style>
