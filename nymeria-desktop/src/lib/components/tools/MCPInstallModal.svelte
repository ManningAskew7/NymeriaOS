<script lang="ts">
  import Modal from '../common/Modal.svelte';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import MCPInstallRecipes from './MCPInstallRecipes.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import type {
    MCPInstallConfigField,
    MCPInstallPreviewResponse,
    MCPInstallResponse,
  } from '$lib/types';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    threadId?: string;
  }

  let { isOpen, onClose, threadId }: Props = $props();

  type Stage = 'input' | 'previewing' | 'preview' | 'installing' | 'success';

  let stage = $state<Stage>('input');
  let source = $state('');
  let bundleFile = $state<File | null>(null);
  let autoEnable = $state(true);
  let confirmed = $state(false);
  let error = $state<string | null>(null);
  let preview = $state<MCPInstallPreviewResponse | null>(null);
  let selectedCandidateId = $state<string | null>(null);
  let result = $state<MCPInstallResponse | null>(null);
  let configValues = $state<Record<string, string>>({});
  let progressStep = $state(0);
  let progressTimer: ReturnType<typeof setInterval> | null = null;
  let fileInput: HTMLInputElement | null = $state(null);

  const FORMAT_LABELS = {
    json: { label: 'Claude Desktop JSON', hint: 'Extracts the selected server entry.' },
    http: { label: 'HTTP URL', hint: 'Uses remote HTTP transport.' },
    npm: { label: 'npm package', hint: 'Runs through npx with an isolated cache.' },
    pypi: { label: 'PyPI package', hint: 'Runs through uvx with an isolated cache.' },
    git: { label: 'Git repository', hint: 'Clones source and needs confirmation.' },
    bundle_url: { label: 'MCP bundle URL', hint: 'Downloads and unpacks a bundle after confirmation.' },
    registry: { label: 'Registry ID', hint: 'Resolves through the MCP registry parser.' },
    stdio: { label: 'stdio command', hint: 'Runs as a local subprocess.' },
    empty: { label: '', hint: '' },
  } as const;

  const REGISTRY_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]*\/[a-zA-Z0-9._-]+$/;

  function detectFormat(src: string): keyof typeof FORMAT_LABELS {
    const s = src.trim();
    if (!s) return 'empty';
    if (s.startsWith('{') || s.startsWith('```')) return 'json';
    if (/^https?:\/\/www\.npmjs\.com\/package\//i.test(s)) return 'npm';
    if (/^https?:\/\/pypi\.org\/project\//i.test(s)) return 'pypi';
    if (/^https?:\/\/.*\.(mcpb|dxt|zip)(\?.*)?$/i.test(s)) return 'bundle_url';
    if (/^https?:\/\/(github\.com|gitlab\.com|bitbucket\.org)\//i.test(s) || /\.git$/i.test(s)) return 'git';
    if (s.startsWith('http://') || s.startsWith('https://')) return 'http';
    if (REGISTRY_ID_RE.test(s)) return 'registry';
    return 'stdio';
  }

  let detectedFormat = $derived(detectFormat(source));
  let canPreview = $derived(stage === 'input' && (source.trim().length > 0 || bundleFile !== null));
  let activeCandidate = $derived.by(() => {
    if (!preview) return null;
    return preview.candidates.find((candidate) => candidate.id === selectedCandidateId) ?? preview.candidates[0] ?? null;
  });
  let activeServer = $derived(activeCandidate?.server ?? preview?.server ?? null);
  let activePlan = $derived(activeCandidate?.plan ?? preview?.plan ?? null);
  let canInstall = $derived(
    stage === 'preview' &&
    preview !== null &&
    activePlan !== null &&
    (!activePlan.confirmation_required || confirmed)
  );

  function resetState() {
    stage = 'input';
    source = '';
    bundleFile = null;
    autoEnable = true;
    confirmed = false;
    error = null;
    preview = null;
    selectedCandidateId = null;
    result = null;
    configValues = {};
    progressStep = 0;
    if (fileInput) fileInput.value = '';
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function handleClose() {
    resetState();
    onClose();
  }

  function startProgress(maxStep: number, intervalMs = 1600) {
    progressStep = 0;
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(() => {
      progressStep = Math.min(progressStep + 1, maxStep);
    }, intervalMs);
  }

  function stopProgress() {
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function seedConfigValues(nextPreview: MCPInstallPreviewResponse) {
    const seeded: Record<string, string> = {};
    const plan = nextPreview.candidates.find((candidate) => candidate.id === selectedCandidateId)?.plan ?? nextPreview.plan;
    for (const field of plan.required_config || []) {
      if (field.default !== undefined && field.default !== null) {
        seeded[field.name] = String(field.default);
      }
    }
    configValues = seeded;
  }

  async function handlePreview() {
    if (!canPreview) return;
    error = null;
    preview = null;
    result = null;
    confirmed = false;
    stage = 'previewing';
    startProgress(1, 1200);

    try {
      const nextPreview = bundleFile
        ? await mcpServersStore.previewUpload(bundleFile)
        : await mcpServersStore.preview({ source: source.trim() });
      preview = nextPreview;
      selectedCandidateId = nextPreview.selectedCandidateId ?? nextPreview.candidates[0]?.id ?? null;
      seedConfigValues(nextPreview);
      stage = 'preview';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Preview failed';
      stage = 'input';
    } finally {
      stopProgress();
    }
  }

  async function handleInstall() {
    if (!preview || !canInstall) return;
    error = null;
    stage = 'installing';
    startProgress(2, 1800);

    try {
      const plan = activePlan;
      const config_values: Record<string, string> = {};
      const credential_values: Record<string, string> = {};
      for (const field of plan?.required_config ?? []) {
        const value = configValues[field.name] ?? '';
        if (!value) continue;
        if (field.sensitive) {
          credential_values[field.name] = value;
        } else {
          config_values[field.name] = value;
        }
      }
      const res = await mcpServersStore.install({
        preview_token: preview.previewToken,
        candidate_id: selectedCandidateId ?? undefined,
        confirmed,
        confirmed_risk_ids: confirmed
          ? (plan?.risk_signals ?? []).filter((signal) => signal.requires_confirmation).map((signal) => signal.id)
          : [],
        config_values,
        credential_values,
        auto_enable: autoEnable,
        thread_id: threadId,
      });
      result = res;
      stage = 'success';
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      const raw = e instanceof Error ? e.message : 'Installation failed';
      if (/failed to fetch|networkerror|load failed/i.test(raw)) {
        error = "Couldn't reach the API, or the response was blocked before the app received backend details.";
      } else {
        error = raw;
      }
      stage = 'preview';
    } finally {
      stopProgress();
    }
  }

  function handlePrefill(value: string) {
    source = value;
    bundleFile = null;
    error = null;
    if (fileInput) fileInput.value = '';
  }

  function handleBundleFile(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    bundleFile = target.files?.[0] ?? null;
    if (bundleFile) source = '';
    error = null;
  }

  function clearBundleFile() {
    bundleFile = null;
    if (fileInput) fileInput.value = '';
  }

  function handleBackToInput() {
    stage = 'input';
    preview = null;
    selectedCandidateId = null;
    result = null;
    confirmed = false;
    error = null;
  }

  function handleInstallAnother() {
    resetState();
  }

  function setConfigValue(name: string, value: string) {
    configValues = { ...configValues, [name]: value };
  }

  function handleCandidateChange(candidateId: string) {
    selectedCandidateId = candidateId;
    if (!preview) return;
    const seeded: Record<string, string> = {};
    const plan = preview.candidates.find((candidate) => candidate.id === candidateId)?.plan ?? preview.plan;
    for (const field of plan.required_config || []) {
      if (field.default !== undefined && field.default !== null) {
        seeded[field.name] = String(field.default);
      }
    }
    configValues = seeded;
    confirmed = false;
  }

  function fieldLabel(field: MCPInstallConfigField): string {
    return field.label || field.name;
  }

  function fieldDescription(field: MCPInstallConfigField): string {
    return field.description || field.env_name || field.header_name || '';
  }

  function fieldInputType(field: MCPInstallConfigField): string {
    return field.sensitive ? 'password' : 'text';
  }

  function riskClass(level: string): string {
    if (level === 'high') return 'risk-high';
    if (level === 'medium') return 'risk-medium';
    return 'risk-low';
  }

  const PREVIEW_STEPS = [
    'Parsing source',
    'Preparing install plan',
  ];

  const INSTALL_STEPS = [
    'Preparing runtime',
    'Starting MCP server',
    'Discovering tools',
  ];
</script>

<Modal title="Install MCP Server" {isOpen} onClose={handleClose}>
  <div class="install-root">
    {#if stage === 'input'}
      <div class="input-grid">
        <div class="input-main">
          <label class="field-label" for="mcp-paste-box">
            <span>MCP source</span>
          </label>

          <div class="paste-box-wrap">
            <textarea
              id="mcp-paste-box"
              class="paste-box"
              bind:value={source}
              placeholder={`npx -y @modelcontextprotocol/server-filesystem ~/Documents

{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}

https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem

https://github.com/example/mcp-server`}
              rows={10}
              spellcheck="false"
              autocomplete="off"
              autocapitalize="off"
              disabled={bundleFile !== null}
            ></textarea>

            {#if detectedFormat !== 'empty' && !bundleFile}
              <div class="format-chip" role="status" aria-live="polite">
                <span class="chip-dot"></span>
                <span>{FORMAT_LABELS[detectedFormat].label}</span>
              </div>
            {/if}
          </div>

          {#if detectedFormat !== 'empty' && !bundleFile}
            <div class="format-hint">{FORMAT_LABELS[detectedFormat].hint}</div>
          {/if}

          <div class="bundle-row">
            <label class="bundle-upload">
              <input
                bind:this={fileInput}
                type="file"
                accept=".mcpb,.dxt,.zip,application/zip"
                onchange={handleBundleFile}
              />
              <Icon name="upload" size={15} />
              <span>{bundleFile ? bundleFile.name : 'Upload MCPB, DXT, or ZIP'}</span>
            </label>
            {#if bundleFile}
              <button type="button" class="clear-file" onclick={clearBundleFile} title="Clear bundle">
                <Icon name="x" size={14} />
              </button>
            {/if}
          </div>

          {#if error}
            <div class="error-banner" role="alert">
              <Icon name="error" size={16} />
              <span>{error}</span>
            </div>
          {/if}

          <div class="options-row">
            <label class="option-check">
              <input type="checkbox" bind:checked={autoEnable} />
              <span class="check-track"><span class="check-thumb"></span></span>
              <span class="option-text">
                Auto-enable discovered tools
                <span class="option-sub">{threadId ? 'for this thread' : 'globally'}</span>
              </span>
            </label>
          </div>

          <div class="actions-row">
            <Button variant="ghost" onclick={handleClose}>Cancel</Button>
            <Button variant="primary" disabled={!canPreview} onclick={handlePreview}>
              <Icon name="bolt" size={14} />
              Preview
            </Button>
          </div>
        </div>

        <div class="input-side">
          <MCPInstallRecipes onUse={handlePrefill} />
        </div>
      </div>

    {:else if stage === 'previewing'}
      <div class="progress-panel">
        <div class="spin-ring"></div>
        <h3>Reviewing MCP source</h3>
        <ol class="progress-list">
          {#each PREVIEW_STEPS as step, i}
            <li class:done={i < progressStep} class:active={i === progressStep}>
              {#if i < progressStep}
                <Icon name="check" size={14} />
              {:else if i === progressStep}
                <span class="dot-pulse"></span>
              {:else}
                <span class="dot-idle"></span>
              {/if}
              <span>{step}</span>
            </li>
          {/each}
        </ol>
      </div>

    {:else if stage === 'preview' && preview && activePlan && activeServer}
      <div class="preview-panel">
        <div class="preview-header">
          <div>
            <h3>{activeServer.name}</h3>
            <p>{activePlan.parsed_summary}</p>
          </div>
          <span class="risk-pill {riskClass(activePlan.risk_level)}">
            {activePlan.risk_level} risk
          </span>
        </div>

        {#if preview.candidates.length > 1}
          <label class="config-field">
            <span>Detected server</span>
            <select
              value={selectedCandidateId ?? ''}
              onchange={(event) => handleCandidateChange((event.currentTarget as HTMLSelectElement).value)}
            >
              {#each preview.candidates as candidate}
                <option value={candidate.id}>{candidate.title}</option>
              {/each}
            </select>
          </label>
        {/if}

        <div class="plan-grid">
          <div class="plan-item">
            <span>Source</span>
            <code>{activePlan.source_type}</code>
          </div>
          <div class="plan-item">
            <span>Runtime</span>
            <code>{activePlan.runtime_type}</code>
          </div>
          <div class="plan-item wide">
            <span>Command</span>
            <code>{activePlan.command_preview || activeServer.url || 'runtime prepares command during install'}</code>
          </div>
        </div>

        {#if activePlan.warnings.length > 0}
          <div class="warning-banner">
            <Icon name="warning" size={16} />
            <div>
              {#each activePlan.warnings as warning}
                <p>{warning}</p>
              {/each}
            </div>
          </div>
        {/if}

        {#if activePlan.required_config.length > 0}
          <div class="config-section">
            <span class="section-title">Required configuration</span>
            <div class="config-fields">
              {#each activePlan.required_config as field}
                <label class="config-field">
                  <span>
                    {fieldLabel(field)}
                    {#if field.required !== false}<strong>*</strong>{/if}
                  </span>
                  <input
                    type={fieldInputType(field)}
                    value={configValues[field.name] ?? ''}
                    placeholder={fieldDescription(field)}
                    oninput={(event) => setConfigValue(field.name, (event.currentTarget as HTMLInputElement).value)}
                  />
                  {#if field.error}
                    <small class="field-error">{field.error}</small>
                  {:else if fieldDescription(field)}
                    <small>{fieldDescription(field)}</small>
                  {/if}
                </label>
              {/each}
            </div>
          </div>
        {/if}

        {#if activePlan.confirmation_required}
          <label class="confirm-box">
            <input type="checkbox" bind:checked={confirmed} />
            <span>
              I approve running this managed MCP setup on the Nymeria host.
            </span>
          </label>
        {/if}

        {#if error}
          <div class="error-banner" role="alert">
            <Icon name="error" size={16} />
            <span>{error}</span>
          </div>
        {/if}

        <div class="actions-row">
          <Button variant="ghost" onclick={handleBackToInput}>Back</Button>
          <Button variant="primary" disabled={!canInstall} onclick={handleInstall}>
            <Icon name="bolt" size={14} />
            Install
          </Button>
        </div>
      </div>

    {:else if stage === 'installing'}
      <div class="progress-panel">
        <div class="spin-ring"></div>
        <h3>Installing MCP server</h3>
        <ol class="progress-list">
          {#each INSTALL_STEPS as step, i}
            <li class:done={i < progressStep} class:active={i === progressStep}>
              {#if i < progressStep}
                <Icon name="check" size={14} />
              {:else if i === progressStep}
                <span class="dot-pulse"></span>
              {:else}
                <span class="dot-idle"></span>
              {/if}
              <span>{step}</span>
            </li>
          {/each}
        </ol>
      </div>

    {:else if stage === 'success' && result}
      <div class="success-panel" class:draft-result={result.status === 'draft'}>
        <div class="success-header">
          <div class="success-icon">
            <Icon name={result.status === 'ok' ? 'success' : 'warning'} size={28} />
          </div>
          <div class="success-heading">
            <h3>{result.status === 'ok' ? `${result.server.name} is installed` : `${result.server.name} was saved as a draft`}</h3>
            <p class="success-sub">{result.discoveryError || result.parsedSummary}</p>
          </div>
        </div>

        <div class="stat-row">
          <div class="stat">
            <span class="stat-label">Server ID</span>
            <code>{result.server.id}</code>
          </div>
          <div class="stat">
            <span class="stat-label">Tools</span>
            <span class="stat-value">{result.discoveredTools}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Status</span>
            <span class="stat-value">{result.server.installStatus}</span>
          </div>
        </div>

        {#if result.toolNames.length > 0}
          <div class="tools-section">
            <div class="tools-head">
              <span class="tools-title">Tools available to the agent</span>
              <span class="tools-count">{result.toolNames.length}</span>
            </div>
            <div class="tool-chips">
              {#each result.toolNames as tn}
                <code class="tool-chip">{tn}</code>
              {/each}
            </div>
          </div>
        {/if}

        {#if result.installLogs.length > 0}
          <details class="log-details">
            <summary>Install log</summary>
            <pre>{result.installLogs.join('\n')}</pre>
          </details>
        {/if}

        <div class="success-actions">
          <Button variant="ghost" onclick={handleInstallAnother}>
            <Icon name="plus" size={14} /> Install another
          </Button>
          <Button variant="primary" onclick={handleClose}>
            <Icon name="check" size={14} /> Done
          </Button>
        </div>
      </div>
    {/if}
  </div>
</Modal>

<style>
  .install-root {
    width: min(980px, 90vw);
    max-height: 80vh;
    overflow-y: auto;
  }

  .input-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(260px, 0.8fr);
    gap: var(--spacing-lg);
  }

  @media (max-width: 820px) {
    .input-grid {
      grid-template-columns: 1fr;
    }
  }

  .input-main,
  .input-side,
  .preview-panel,
  .success-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: 0;
  }

  .field-label {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .paste-box-wrap {
    position: relative;
  }

  .paste-box {
    width: 100%;
    min-height: 250px;
    padding: var(--spacing-md) var(--spacing-md) calc(var(--spacing-md) + 28px);
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.5;
    resize: vertical;
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .paste-box:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .paste-box:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb, 108, 159, 255), 0.15);
  }

  .paste-box::placeholder {
    color: var(--text-muted);
    opacity: 0.75;
  }

  .format-chip {
    position: absolute;
    bottom: var(--spacing-sm);
    left: var(--spacing-sm);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px var(--spacing-sm);
    background: rgba(var(--accent-primary-rgb, 108, 159, 255), 0.15);
    color: var(--accent-primary);
    border: 1px solid rgba(var(--accent-primary-rgb, 108, 159, 255), 0.3);
    border-radius: var(--radius-full, 9999px);
    font-size: var(--font-size-xs);
    font-weight: 500;
    pointer-events: none;
  }

  .chip-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-primary);
  }

  .format-hint {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    padding: 0 var(--spacing-xs);
  }

  .bundle-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .bundle-upload {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: fit-content;
    max-width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    background: var(--bg-elevated);
    cursor: pointer;
    font-size: var(--font-size-sm);
  }

  .bundle-upload:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .bundle-upload input {
    display: none;
  }

  .bundle-upload span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .clear-file {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
  }

  .clear-file:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .error-banner,
  .warning-banner {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .error-banner {
    background: rgba(244, 67, 54, 0.1);
    color: var(--error, #f44336);
    border: 1px solid rgba(244, 67, 54, 0.3);
  }

  .warning-banner {
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
    border: 1px solid rgba(245, 158, 11, 0.3);
  }

  .warning-banner p {
    margin: 0;
  }

  .options-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
  }

  .option-check {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-sm);
    cursor: pointer;
    user-select: none;
  }

  .option-check input {
    display: none;
  }

  .check-track {
    position: relative;
    width: 32px;
    height: 18px;
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: 9px;
    transition: all var(--transition-fast);
    flex-shrink: 0;
  }

  .check-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: var(--text-muted);
    border-radius: 50%;
    transition: all var(--transition-fast);
  }

  .option-check input:checked + .check-track {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .option-check input:checked + .check-track .check-thumb {
    left: 16px;
    background: white;
  }

  .option-text {
    display: inline-flex;
    align-items: baseline;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
  }

  .option-sub {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .actions-row,
  .success-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  .progress-panel {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md);
    min-height: 320px;
    text-align: center;
  }

  .spin-ring {
    width: 48px;
    height: 48px;
    border: 3px solid var(--border-subtle);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .progress-panel h3 {
    margin: 0;
    font-size: var(--font-size-lg);
    color: var(--text-primary);
  }

  .progress-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    width: 100%;
    max-width: 360px;
    text-align: left;
  }

  .progress-list li {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .progress-list li.active {
    color: var(--text-primary);
  }

  .progress-list li.done {
    color: var(--accent-primary);
  }

  .dot-idle,
  .dot-pulse {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .dot-idle {
    background: var(--border-default);
  }

  .dot-pulse {
    background: var(--accent-primary);
    animation: pulse 1.4s ease-out infinite;
  }

  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(var(--accent-primary-rgb, 108, 159, 255), 0.5); }
    70% { box-shadow: 0 0 0 8px rgba(var(--accent-primary-rgb, 108, 159, 255), 0); }
    100% { box-shadow: 0 0 0 0 rgba(var(--accent-primary-rgb, 108, 159, 255), 0); }
  }

  .preview-header,
  .success-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .preview-header h3,
  .success-heading h3 {
    margin: 0 0 4px 0;
    font-size: var(--font-size-lg);
    color: var(--text-primary);
  }

  .preview-header p,
  .success-sub {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .risk-pill {
    flex-shrink: 0;
    padding: 3px var(--spacing-sm);
    border-radius: var(--radius-full, 9999px);
    border: 1px solid var(--border-subtle);
    font-size: var(--font-size-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .risk-low {
    color: #22c55e;
    background: rgba(34, 197, 94, 0.12);
    border-color: rgba(34, 197, 94, 0.3);
  }

  .risk-medium {
    color: #f59e0b;
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.3);
  }

  .risk-high {
    color: #f44336;
    background: rgba(244, 67, 54, 0.12);
    border-color: rgba(244, 67, 54, 0.3);
  }

  .plan-grid,
  .stat-row {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--spacing-sm);
  }

  .plan-item,
  .stat {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    min-width: 0;
  }

  .plan-item.wide {
    grid-column: 1 / -1;
  }

  .plan-item span,
  .stat-label {
    font-size: var(--font-size-3xs);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
  }

  .plan-item code,
  .stat code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    word-break: break-all;
  }

  @media (max-width: 640px) {
    .plan-grid,
    .stat-row {
      grid-template-columns: 1fr;
    }
  }

  .section-title,
  .tools-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .config-section,
  .tools-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .config-fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: var(--spacing-sm);
  }

  .config-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .config-field span {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .config-field strong {
    color: var(--error, #f44336);
    margin-left: 2px;
  }

  .config-field input,
  .config-field select {
    width: 100%;
    min-height: 34px;
    padding: 0 var(--spacing-sm);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
  }

  .config-field input:focus,
  .config-field select:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .config-field small {
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
    line-height: 1.3;
  }

  .field-error {
    color: var(--error, #f44336) !important;
  }

  .confirm-box {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .confirm-box input {
    margin-top: 2px;
  }

  .success-panel {
    width: min(680px, 90vw);
  }

  .draft-result .success-icon {
    background: rgba(245, 158, 11, 0.15);
    color: #f59e0b;
  }

  .success-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: rgba(34, 197, 94, 0.15);
    color: #22c55e;
    flex-shrink: 0;
  }

  .success-heading {
    flex: 1;
    min-width: 0;
  }

  .stat-value {
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .tools-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }

  .tools-count {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb, 108, 159, 255), 0.15);
    padding: 2px var(--spacing-sm);
    border-radius: var(--radius-full, 9999px);
  }

  .tool-chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-xs);
    max-height: 200px;
    overflow-y: auto;
    padding: 2px;
  }

  .tool-chip {
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    padding: 4px var(--spacing-sm);
    background: var(--bg-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    word-break: break-all;
  }

  .log-details {
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    overflow: hidden;
  }

  .log-details summary {
    cursor: pointer;
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .log-details pre {
    margin: 0;
    padding: var(--spacing-sm) var(--spacing-md);
    max-height: 220px;
    overflow: auto;
    border-top: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-2xs);
    line-height: 1.4;
    white-space: pre-wrap;
  }
</style>
