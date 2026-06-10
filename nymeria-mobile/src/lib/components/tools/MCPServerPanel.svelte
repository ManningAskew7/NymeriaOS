<script lang="ts">
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import type { MCPServer, MCPServerCreateRequest } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';
  import MCPServerForm from './MCPServerForm.svelte';
  import MCPInstallModal from './MCPInstallModal.svelte';

  interface Props {
    threadId?: string;
  }
  let { threadId }: Props = $props();

  let showAddForm = $state(false);
  let showInstallModal = $state(false);
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

  $effect(() => {
    if (!mcpServersStore.loaded && !mcpServersStore.loading) {
      mcpServersStore.load();
    }
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
  });

  let enabledToolNames = $derived(new Set(defaultToolsStore.defaultToolNames));
  let isAdmin = $derived(configStore.identity?.role === 'admin');

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
    if (server.installStatus === 'needs_config' || server.installStatus === 'draft') return 'var(--warning)';
    if (server.installStatus === 'disabled') return 'var(--text-secondary)';
    if (!server.enabled) return 'var(--text-secondary)';
    if (server.discoveredTools.length === 0) return 'var(--warning)';
    return 'var(--success)';
  }

  function getStatusLabel(server: MCPServer): string {
    if (server.installStatus === 'failed') return 'failed';
    if (server.installStatus === 'needs_config') return 'needs config';
    if (server.installStatus === 'disabled' || !server.enabled) return 'disabled';
    if (server.installStatus === 'draft') return 'draft';
    if (server.discoveredTools.length === 0) return 'no tools';
    return 'ready';
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

  async function toggleGlobalTool(serverId: string, toolName: string) {
    const mcpName = getMcpToolName(serverId, toolName);
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
    }
  }

  async function handleTest(serverId: string) {
    testingServer = serverId;
    delete testResults[serverId];
    try {
      const result = await mcpServersStore.test(serverId);
      testResults = { ...testResults, [serverId]: result };
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
    } finally {
      discoveringServer = null;
    }
  }

  async function handleRetry(server: MCPServer) {
    retryingServer = server.id;
    try {
      const result = await mcpServersStore.retry(server.id, {
        confirmed: true,
        confirmed_risk_ids: (server.riskSignals || [])
          .filter((signal) => signal.requires_confirmation)
          .map((signal) => signal.id),
      });
      testResults = {
        ...testResults,
        [server.id]: {
          status: result.status === 'ok' ? 'ok' : 'error',
          error: result.discoveryError || result.server.lastError,
          toolsCount: result.discoveredTools,
        },
      };
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      testResults = { ...testResults, [server.id]: { status: 'error', error: humanizeErrorText(e, { action: 'load', resource: "the server's tools" }) } };
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
    }
  }
</script>

<div class="mcp-panel">
  <div class="panel-header">
    <h3>MCP Servers</h3>
    {#if isAdmin}
      <div class="header-actions">
        <button class="add-btn" onclick={() => { showInstallModal = true; }}>
          <Icon name="bolt" size={16} />
        </button>
        <button class="add-btn" onclick={() => { showAddForm = !showAddForm; addError = null; }}>
          <Icon name={showAddForm ? 'x' : 'plus'} size={16} />
        </button>
      </div>
    {/if}
  </div>

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
    <div class="loading"><Spinner size="sm" /></div>
  {:else if mcpServersStore.servers.length === 0 && !showAddForm}
    <p class="empty">No MCP servers configured.</p>
  {:else}
    {#each mcpServersStore.servers as server (server.id)}
      <div class="server-item" class:disabled-server={!server.enabled}>
        <button class="server-row" onclick={() => expandedServer = expandedServer === server.id ? null : server.id}>
          <span class="status-dot" style="background: {getStatusColor(server)}"></span>
          <div class="server-meta">
            <span class="name">{server.name}</span>
            <span class="server-subtitle">{getServerSubtitle(server)} · {getStatusLabel(server)}</span>
          </div>
          <span class="count">{server.discoveredTools.length} tools</span>
          <span class="updated">{timeAgo(server.updatedAt)}</span>
          <Icon name={expandedServer === server.id ? 'chevronDown' : 'chevronRight'} size={14} />
        </button>

        {#if expandedServer === server.id}
          <div class="server-detail">
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
              <div class="info-row">
                <span class="label">Runtime ID</span>
                <code>{server.id}</code>
              </div>

              <div class="info-row">
                <span class="label">Enabled</span>
                <label class="toggle-label">
                  <input type="checkbox" checked={server.enabled} onchange={() => handleToggleEnabled(server)} />
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>

              <code>{server.transport === 'http' ? server.url : `${server.serverCommand} ${server.serverArgs.join(' ')}`}</code>

              {#if server.lastError}
                <div class="test-banner test-fail">{server.lastError}</div>
              {/if}

              {#if testResults[server.id]}
                {@const result = testResults[server.id]}
                <div class="test-banner" class:test-ok={result.status === 'ok'} class:test-fail={result.status === 'error'}>
                  {#if result.status === 'ok'}
                    Connected. {result.toolsCount} tools
                  {:else}
                    {result.error}
                  {/if}
                </div>
              {/if}

              {#if server.discoveredTools.length > 0}
                <div class="tools-section">
                  <span class="tools-heading">Discovered Tools</span>
                  {#each server.discoveredTools as tool}
                    {@const mcpName = getMcpToolName(server.id, tool.name)}
                    {@const isEnabled = enabledToolNames.has(mcpName)}
                    <div class="tool-row" class:tool-enabled={isEnabled}>
                      <div class="tool-info">
                        <span class="tool-name" title={mcpName}>{tool.name}</span>
                        {#if tool.description}
                          <span class="tool-desc">{tool.description}</span>
                        {/if}
                      </div>
                      <label class="toggle-label">
                        <input type="checkbox" checked={isEnabled} onchange={() => toggleGlobalTool(server.id, tool.name)} />
                        <span class="toggle-track"><span class="toggle-thumb"></span></span>
                      </label>
                    </div>
                  {/each}
                </div>
              {/if}

              <div class="action-row">
                <button class="action-btn" onclick={() => handleTest(server.id)} disabled={testingServer === server.id}>
                  {testingServer === server.id ? 'Testing...' : 'Test'}
                </button>
                <button class="action-btn" onclick={() => handleDiscover(server.id)} disabled={discoveringServer === server.id}>
                  {discoveringServer === server.id ? '...' : 'Rediscover'}
                </button>
                {#if server.installStatus === 'failed' || server.installStatus === 'needs_config' || server.installStatus === 'draft'}
                  <button class="action-btn" onclick={() => handleRetry(server)} disabled={retryingServer === server.id}>
                    {retryingServer === server.id ? 'Retrying...' : 'Retry'}
                  </button>
                {/if}
                <button class="action-btn" onclick={() => { editingServerId = server.id; editError = null; }}>
                  Edit server
                </button>
                {#if confirmDeleteId === server.id}
                  <span class="confirm-text">Delete?</span>
                  <button class="action-btn danger" onclick={() => handleDelete(server.id)}>Delete server</button>
                  <button class="action-btn" onclick={() => confirmDeleteId = null}>Cancel</button>
                {:else}
                  <button class="action-btn danger" onclick={() => confirmDeleteId = server.id}>
                    <Icon name="trash" size={14} />
                  </button>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  {/if}
</div>

<style>
  .mcp-panel {
    padding: 0.5rem 0;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }

  .panel-header h3 {
    margin: 0;
    font-size: 0.9rem;
    color: var(--text-primary);
  }

  .add-btn {
    background: none;
    border: none;
    color: var(--accent-primary);
    padding: 0.3rem;
    cursor: pointer;
  }

  .loading, .empty {
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85rem;
    padding: 1rem;
  }

  .server-item {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    margin-bottom: 0.4rem;
    overflow: hidden;
  }

  .server-item.disabled-server {
    opacity: 0.6;
  }

  .server-row {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    background: var(--bg-elevated);
    border: none;
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.85rem;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .server-meta {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    min-width: 0;
  }

  .name {
    font-weight: 500;
    line-height: 1.2;
  }

  .server-subtitle {
    font-size: 0.7rem;
    color: var(--text-secondary);
  }

  .count {
    font-size: 0.75rem;
    color: var(--text-secondary);
    background: var(--bg-base);
    padding: 0.1rem 0.4rem;
    border-radius: 10px;
    white-space: nowrap;
  }

  .updated {
    font-size: 0.7rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .server-detail {
    padding: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .info-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.85rem;
  }

  .label {
    color: var(--text-secondary);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    cursor: pointer;
  }

  .toggle-label input {
    display: none;
  }

  .toggle-track {
    position: relative;
    width: 32px;
    height: 18px;
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: 9px;
    transition: all 0.15s;
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: var(--text-secondary);
    border-radius: 50%;
    transition: all 0.15s;
  }

  .toggle-label input:checked + .toggle-track {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .toggle-label input:checked + .toggle-track .toggle-thumb {
    left: 16px;
    background: white;
  }

  .server-detail code {
    font-size: 0.75rem;
    word-break: break-all;
  }

  .test-banner {
    padding: 0.4rem;
    border-radius: 4px;
    font-size: 0.8rem;
  }

  .test-ok {
    background: rgba(var(--success-rgb), 0.12);
    color: #22c55e;
  }

  .test-fail {
    background: rgba(var(--error-rgb), 0.12);
    color: var(--error);
  }

  .tools-section {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .tools-heading {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.4rem;
    background: var(--bg-elevated);
    border-radius: 4px;
  }

  .tool-row.tool-enabled {
    border-left: 2px solid var(--accent-primary);
  }

  .tool-info {
    flex: 1;
    min-width: 0;
  }

  .tool-name {
    display: block;
    color: var(--accent-primary);
    font-family: monospace;
    font-size: 0.75rem;
  }

  .tool-desc {
    display: block;
    color: var(--text-secondary);
    font-size: 0.7rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .action-row {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
    justify-content: flex-end;
    padding-top: 0.3rem;
    border-top: 1px solid var(--border-default);
  }

  .action-btn {
    padding: 0.3rem 0.6rem;
    border: none;
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: 0.8rem;
    border-radius: 4px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.2rem;
    min-height: var(--touch-target-min);
  }

  .action-btn:disabled {
    opacity: 0.5;
  }

  .action-btn.danger {
    color: var(--error);
  }

  .confirm-text {
    font-size: 0.8rem;
    color: var(--error);
    display: flex;
    align-items: center;
  }
</style>
