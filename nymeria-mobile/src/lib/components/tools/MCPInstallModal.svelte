<script lang="ts">
  import Modal from '$lib/components/common/Modal.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import type { MCPInstallConfigField, MCPInstallPreviewResponse, MCPInstallResponse } from '$lib/types';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    threadId?: string;
  }

  let { isOpen, onClose, threadId }: Props = $props();

  type Stage = 'input' | 'previewing' | 'preview' | 'installing' | 'success';

  let stage = $state<Stage>('input');
  let source = $state('');
  let autoEnable = $state(true);
  let confirmed = $state(false);
  let error = $state<string | null>(null);
  let preview = $state<MCPInstallPreviewResponse | null>(null);
  let selectedCandidateId = $state<string | null>(null);
  let result = $state<MCPInstallResponse | null>(null);
  let configValues = $state<Record<string, string>>({});

  let activeCandidate = $derived.by(() => {
    if (!preview) return null;
    return preview.candidates.find((candidate) => candidate.id === selectedCandidateId) ?? preview.candidates[0] ?? null;
  });
  let activeServer = $derived(activeCandidate?.server ?? preview?.server ?? null);
  let activePlan = $derived(activeCandidate?.plan ?? preview?.plan ?? null);
  let canPreview = $derived(stage === 'input' && source.trim().length > 0);
  let canInstall = $derived(stage === 'preview' && activePlan !== null && (!activePlan.confirmation_required || confirmed));

  function visibleToolNames(response: MCPInstallResponse): string[] {
    return response.toolDisplayNames.length > 0 ? response.toolDisplayNames : response.toolNames;
  }

  function resetState() {
    stage = 'input';
    source = '';
    autoEnable = true;
    confirmed = false;
    error = null;
    preview = null;
    selectedCandidateId = null;
    result = null;
    configValues = {};
  }

  function handleClose() {
    resetState();
    onClose();
  }

  function seedConfigValues(nextPreview: MCPInstallPreviewResponse) {
    const plan = nextPreview.candidates.find((candidate) => candidate.id === selectedCandidateId)?.plan ?? nextPreview.plan;
    const seeded: Record<string, string> = {};
    for (const field of plan.required_config || []) {
      if (field.default !== undefined && field.default !== null) {
        seeded[field.name] = String(field.default);
      }
    }
    configValues = seeded;
  }

  async function handlePreview() {
    if (!canPreview) return;
    stage = 'previewing';
    error = null;
    try {
      const nextPreview = await mcpServersStore.preview({ source: source.trim() });
      preview = nextPreview;
      selectedCandidateId = nextPreview.selectedCandidateId ?? nextPreview.candidates[0]?.id ?? null;
      seedConfigValues(nextPreview);
      stage = 'preview';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Preview failed';
      stage = 'input';
    }
  }

  async function handleInstall() {
    if (!preview || !activePlan || !canInstall) return;
    stage = 'installing';
    error = null;
    const config_values: Record<string, string> = {};
    const credential_values: Record<string, string> = {};
    for (const field of activePlan.required_config || []) {
      const value = configValues[field.name] ?? '';
      if (!value) continue;
      if (field.sensitive) {
        credential_values[field.name] = value;
      } else {
        config_values[field.name] = value;
      }
    }
    try {
      result = await mcpServersStore.install({
        preview_token: preview.previewToken,
        candidate_id: selectedCandidateId ?? undefined,
        confirmed,
        confirmed_risk_ids: confirmed
          ? activePlan.risk_signals.filter((signal) => signal.requires_confirmation).map((signal) => signal.id)
          : [],
        config_values,
        credential_values,
        auto_enable: autoEnable,
        thread_id: threadId,
      });
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
      stage = 'success';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Installation failed';
      stage = 'preview';
    }
  }

  function handleCandidateChange(candidateId: string) {
    selectedCandidateId = candidateId;
    confirmed = false;
    if (preview) seedConfigValues(preview);
  }

  function setConfigValue(name: string, value: string) {
    configValues = { ...configValues, [name]: value };
  }

  function fieldLabel(field: MCPInstallConfigField): string {
    return field.label || field.name;
  }

  function fieldDescription(field: MCPInstallConfigField): string {
    return field.description || field.env_name || field.header_name || '';
  }
</script>

<Modal title="Install MCP Server" isOpen={isOpen} onClose={handleClose}>
  <div class="install-flow">
    {#if stage === 'input'}
      <textarea
        class="source-box"
        bind:value={source}
        placeholder={`npx -y @modelcontextprotocol/server-filesystem ~/Documents

{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}

https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem`}
        rows={9}
        spellcheck="false"
      ></textarea>

      <label class="check-row">
        <input type="checkbox" bind:checked={autoEnable} />
        <span>Auto-enable discovered tools</span>
      </label>

      {#if error}<div class="error-banner">{error}</div>{/if}

      <div class="action-row">
        <button type="button" class="secondary" onclick={handleClose}>Cancel</button>
        <button type="button" class="primary" disabled={!canPreview} onclick={handlePreview}>
          <Icon name="bolt" size={15} /> Preview
        </button>
      </div>
    {:else if stage === 'previewing' || stage === 'installing'}
      <div class="progress-state">
        <div class="spinner"></div>
        <p>{stage === 'previewing' ? 'Reviewing source...' : 'Installing server...'}</p>
      </div>
    {:else if stage === 'preview' && activePlan && activeServer && preview}
      <div class="preview-card">
        <div class="preview-head">
          <div>
            <h3>{activeServer.name}</h3>
            <p>{activePlan.parsed_summary}</p>
          </div>
          <span class="risk">{activePlan.risk_level}</span>
        </div>

        {#if preview.candidates.length > 1}
          <label class="field">
            <span>Detected server</span>
            <select value={selectedCandidateId ?? ''} onchange={(event) => handleCandidateChange((event.currentTarget as HTMLSelectElement).value)}>
              {#each preview.candidates as candidate}
                <option value={candidate.id}>{candidate.title}</option>
              {/each}
            </select>
          </label>
        {/if}

        <div class="meta-grid">
          <span>Source</span><code>{activePlan.source_type}</code>
          <span>Runtime</span><code>{activePlan.runtime_type}</code>
          <span>Command</span><code>{activePlan.command_preview || activeServer.url || 'prepared during install'}</code>
        </div>

        {#if activePlan.warnings.length > 0}
          <div class="warning-list">
            {#each activePlan.warnings as warning}
              <p>{warning}</p>
            {/each}
          </div>
        {/if}

        {#if activePlan.required_config.length > 0}
          <div class="fields">
            {#each activePlan.required_config as field}
              <label class="field">
                <span>{fieldLabel(field)}{field.required !== false ? ' *' : ''}</span>
                <input
                  type={field.sensitive ? 'password' : 'text'}
                  value={configValues[field.name] ?? ''}
                  placeholder={fieldDescription(field)}
                  oninput={(event) => setConfigValue(field.name, (event.currentTarget as HTMLInputElement).value)}
                />
                {#if field.error}<small>{field.error}</small>{/if}
              </label>
            {/each}
          </div>
        {/if}

        {#if activePlan.confirmation_required}
          <label class="confirm-row">
            <input type="checkbox" bind:checked={confirmed} />
            <span>I approve running this MCP setup on the Nymeria host.</span>
          </label>
        {/if}

        {#if error}<div class="error-banner">{error}</div>{/if}

        <div class="action-row">
          <button type="button" class="secondary" onclick={() => { stage = 'input'; preview = null; }}>Back</button>
          <button type="button" class="primary" disabled={!canInstall} onclick={handleInstall}>
            <Icon name="bolt" size={15} /> Install
          </button>
        </div>
      </div>
    {:else if stage === 'success' && result}
      <div class="success-state">
        <Icon name={result.status === 'ok' ? 'success' : 'warning'} size={28} />
        <h3>{result.server.name}</h3>
        <p>{result.discoveryError || `${result.discoveredTools} tools discovered`}</p>
        {#if visibleToolNames(result).length > 0}
          <div class="chips">
            {#each visibleToolNames(result) as toolName, index}
              <code title={result.toolNames[index] ?? toolName}>{toolName}</code>
            {/each}
          </div>
        {/if}
        <button type="button" class="primary full" onclick={handleClose}>Close</button>
      </div>
    {/if}
  </div>
</Modal>

<style>
  .install-flow {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-height: 100%;
  }

  .source-box,
  .field input,
  .field select {
    width: 100%;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
  }

  .source-box {
    min-height: 220px;
    padding: var(--spacing-sm);
    font-family: var(--font-mono);
    font-size: 13px;
    resize: vertical;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .field span,
  .meta-grid span {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .field input,
  .field select {
    min-height: var(--touch-target-min);
    padding: 0 var(--spacing-sm);
  }

  .check-row,
  .confirm-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .preview-card,
  .success-state,
  .progress-state {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .preview-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .preview-head h3,
  .success-state h3 {
    margin: 0;
    font-size: var(--font-size-lg);
  }

  .preview-head p,
  .success-state p {
    margin: 0.25rem 0 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .risk {
    padding: 0.2rem 0.45rem;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .meta-grid {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 0.45rem;
    align-items: baseline;
  }

  code {
    font-family: var(--font-mono);
    font-size: 12px;
    word-break: break-word;
  }

  .warning-list,
  .error-banner {
    padding: var(--spacing-sm);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .warning-list {
    border: 1px solid rgba(245, 158, 11, 0.35);
    color: #f59e0b;
    background: rgba(245, 158, 11, 0.08);
  }

  .warning-list p {
    margin: 0.2rem 0;
  }

  .error-banner {
    border: 1px solid rgba(var(--error-rgb), 0.35);
    color: var(--error);
    background: rgba(var(--error-rgb), 0.08);
  }

  .action-row {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: auto;
  }

  button {
    min-height: var(--touch-target-min);
    border-radius: var(--radius-md);
    padding: 0 var(--spacing-md);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.35rem;
  }

  button:disabled {
    opacity: 0.55;
  }

  .primary {
    background: var(--accent-primary);
    color: var(--text-on-accent);
  }

  .secondary {
    color: var(--text-secondary);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
  }

  .full {
    width: 100%;
  }

  .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-subtle);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 800ms linear infinite;
  }

  .chips {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .chips code {
    padding: 0.35rem 0.45rem;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
  }
</style>
