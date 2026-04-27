<script lang="ts">
  import Modal from '../common/Modal.svelte';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import MCPInstallRecipes from './MCPInstallRecipes.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import type { MCPInstallResponse } from '$lib/types';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    threadId?: string;
  }

  let { isOpen, onClose, threadId }: Props = $props();

  type Stage = 'input' | 'installing' | 'success';

  let stage = $state<Stage>('input');
  let source = $state('');
  let autoEnable = $state(true);
  let error = $state<string | null>(null);
  let result = $state<MCPInstallResponse | null>(null);
  let progressStep = $state(0);
  let progressTimer: ReturnType<typeof setInterval> | null = null;

  const FORMAT_LABELS = {
    json: { label: 'Claude Desktop JSON', hint: 'We detected a config block and will extract the server entry.' },
    url: { label: 'HTTP URL', hint: 'We\'ll connect over HTTP/SSE transport.' },
    registry: { label: 'Registry ID', hint: 'We\'ll resolve this on the official MCP registry.' },
    stdio: { label: 'stdio command', hint: 'We\'ll run this as a local subprocess.' },
    empty: { label: '', hint: '' },
  } as const;

  const REGISTRY_ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]*\/[a-zA-Z0-9._-]+$/;

  function detectFormat(src: string): keyof typeof FORMAT_LABELS {
    const s = src.trim();
    if (!s) return 'empty';
    if (s.startsWith('{')) return 'json';
    if (s.startsWith('http://') || s.startsWith('https://')) return 'url';
    if (REGISTRY_ID_RE.test(s)) return 'registry';
    return 'stdio';
  }

  let detectedFormat = $derived(detectFormat(source));
  let canInstall = $derived(detectedFormat !== 'empty' && stage === 'input');

  function resetState() {
    stage = 'input';
    source = '';
    autoEnable = true;
    error = null;
    result = null;
    progressStep = 0;
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function handleClose() {
    resetState();
    onClose();
  }

  async function handleInstall() {
    if (!canInstall) return;
    error = null;
    stage = 'installing';
    progressStep = 0;

    // Cosmetic staged progress — the POST is atomic but takes 5-15s.
    progressTimer = setInterval(() => {
      progressStep = Math.min(progressStep + 1, 2);
    }, 1800);

    try {
      const res = await mcpServersStore.install({
        source: source.trim(),
        auto_enable: autoEnable,
        thread_id: threadId,
      });
      result = res;
      stage = 'success';
      // Refresh the global default tools list so the Tools page picks up the new mcp__* names.
      defaultToolsStore.resetLoaded();
      await defaultToolsStore.load();
    } catch (e) {
      const raw = e instanceof Error ? e.message : 'Installation failed';
      // fetch() throws TypeError on network-level failures: DNS, connection
      // reset, CORS preflight abort, or a reverse proxy swapping our JSON
      // error body for a branded 5xx error page with missing CORS headers.
      // In all those cases the browser never sees the backend's detail.
      if (/failed to fetch|networkerror|load failed/i.test(raw)) {
        error = "Couldn't reach the API, or the response was blocked by something between the app and the backend (proxy / tunnel / firewall). Check the backend logs for the real error and retry.";
      } else {
        error = raw;
      }
      stage = 'input';
    } finally {
      if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
      }
    }
  }

  function handlePrefill(value: string) {
    source = value;
    error = null;
  }

  function handleInstallAnother() {
    resetState();
  }

  const PROGRESS_STEPS = [
    'Parsing what you pasted…',
    'Starting the MCP server…',
    'Asking it what tools it offers…',
  ];
</script>

<Modal title="Install MCP Server" {isOpen} onClose={handleClose}>
  <div class="install-root">
    {#if stage === 'input'}
      <div class="install-grid">
        <!-- Left: paste box -->
        <div class="install-left">
          <label class="paste-label" for="mcp-paste-box">
            <span class="paste-label-title">Paste your MCP configuration</span>
            <span class="paste-label-sub">JSON, a command, a URL, or a registry ID. All four work.</span>
          </label>

          <div class="paste-box-wrap">
            <textarea
              id="mcp-paste-box"
              class="paste-box"
              bind:value={source}
              placeholder={`Examples:

npx -y @modelcontextprotocol/server-filesystem ~/Documents

{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}

https://example.com/mcp

io.github.modelcontextprotocol/server-memory`}
              rows={10}
              spellcheck="false"
              autocomplete="off"
              autocapitalize="off"
            ></textarea>

            {#if detectedFormat !== 'empty'}
              <div class="format-chip" role="status" aria-live="polite">
                <span class="chip-dot"></span>
                <span class="chip-label">Detected:</span>
                <span class="chip-value">{FORMAT_LABELS[detectedFormat].label}</span>
              </div>
            {/if}
          </div>

          {#if detectedFormat !== 'empty'}
            <div class="format-hint">{FORMAT_LABELS[detectedFormat].hint}</div>
          {/if}

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
                {#if threadId}
                  <span class="option-sub">for this thread</span>
                {:else}
                  <span class="option-sub">globally</span>
                {/if}
              </span>
            </label>
          </div>

          <div class="actions-row">
            <Button variant="ghost" onclick={handleClose}>Cancel</Button>
            <Button variant="primary" disabled={!canInstall} onclick={handleInstall}>
              <Icon name="bolt" size={14} />
              Install
            </Button>
          </div>
        </div>

        <!-- Right: help + recipes -->
        <div class="install-right">
          <div class="help-card">
            <div class="help-title">
              <Icon name="info" size={16} />
              <span>How this works</span>
            </div>
            <ol class="help-steps">
              <li>
                <span class="step-num">1</span>
                <div>
                  <div class="step-head">Find an MCP server</div>
                  <div class="step-body">
                    Browse <a href="https://modelcontextprotocol.io/servers" target="_blank" rel="noopener noreferrer">modelcontextprotocol.io/servers</a>,
                    a tutorial, or someone's blog. Copy <em>any one</em> of: their JSON config, a <code>npx</code>/<code>uvx</code>/<code>python</code> command, a server URL, or a registry name.
                  </div>
                </div>
              </li>
              <li>
                <span class="step-num">2</span>
                <div>
                  <div class="step-head">Paste it on the left</div>
                  <div class="step-body">The chip under the box tells you what format we detected.</div>
                </div>
              </li>
              <li>
                <span class="step-num">3</span>
                <div>
                  <div class="step-head">Click Install</div>
                  <div class="step-body">Nymeria starts the server, asks it what tools it offers, and lists them.</div>
                </div>
              </li>
              <li>
                <span class="step-num">4</span>
                <div>
                  <div class="step-head">You're done</div>
                  <div class="step-body">New tools show up in the next screen and stay available for the agent.</div>
                </div>
              </li>
            </ol>
          </div>

          <MCPInstallRecipes onUse={handlePrefill} />
        </div>
      </div>

    {:else if stage === 'installing'}
      <div class="installing-panel">
        <div class="installing-spinner">
          <div class="spin-ring"></div>
        </div>
        <h3>Installing your MCP server</h3>
        <ol class="progress-list">
          {#each PROGRESS_STEPS as step, i}
            <li class:done={i < progressStep} class:active={i === progressStep}>
              {#if i < progressStep}
                <Icon name="check" size={14} />
              {:else if i === progressStep}
                <span class="dot-pulse"></span>
              {:else}
                <span class="dot-idle"></span>
              {/if}
              <span class="step-text">{step}</span>
            </li>
          {/each}
        </ol>
        <p class="installing-note">First install of a server can take 10–15 seconds while <code>npx</code>/<code>uvx</code> downloads it.</p>
      </div>

    {:else if stage === 'success' && result}
      <div class="success-panel">
        <div class="success-header">
          <div class="success-icon">
            <Icon name="success" size={28} />
          </div>
          <div class="success-heading">
            <h3>{result.server.name} is installed</h3>
            <p class="success-sub">{result.parsedSummary}</p>
          </div>
        </div>

        <div class="stat-row">
          <div class="stat">
            <span class="stat-label">Server ID</span>
            <code>{result.server.id}</code>
          </div>
          <div class="stat">
            <span class="stat-label">Tools discovered</span>
            <span class="stat-value">{result.discoveredTools}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Scope</span>
            <span class="stat-value">
              {#if result.threadId}
                This thread
              {:else if autoEnable}
                Globally
              {:else}
                Disabled by default
              {/if}
            </span>
          </div>
        </div>

        {#if result.toolNames.length > 0}
          <div class="tools-section">
            <div class="tools-head">
              <span class="tools-title">New tools available to the agent</span>
              <span class="tools-count">{result.toolNames.length}</span>
            </div>
            <div class="tool-chips">
              {#each result.toolNames as tn}
                <code class="tool-chip">{tn}</code>
              {/each}
            </div>
          </div>
        {:else}
          <div class="no-tools-note">
            <Icon name="warning" size={16} />
            <span>The server started but didn't report any tools. You can rediscover from the MCP Servers panel.</span>
          </div>
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

  /* ---- Input stage ---- */

  .install-grid {
    display: grid;
    grid-template-columns: 1.2fr 1fr;
    gap: var(--spacing-lg);
  }

  @media (max-width: 820px) {
    .install-grid {
      grid-template-columns: 1fr;
    }
  }

  .install-left {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: 0;
  }

  .paste-label {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .paste-label-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .paste-label-sub {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .paste-box-wrap {
    position: relative;
  }

  .paste-box {
    width: 100%;
    min-height: 220px;
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

  .paste-box:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb, 108, 159, 255), 0.15);
  }

  .paste-box::placeholder {
    color: var(--text-muted);
    opacity: 0.7;
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
    animation: chipIn 150ms ease;
    pointer-events: none;
  }

  @keyframes chipIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .chip-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb, 108, 159, 255), 0.25);
  }

  .chip-label {
    color: var(--text-secondary);
    font-weight: 400;
  }

  .chip-value {
    font-family: var(--font-mono);
  }

  .format-hint {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    padding: 0 var(--spacing-xs);
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: rgba(244, 67, 54, 0.1);
    color: var(--error, #f44336);
    border: 1px solid rgba(244, 67, 54, 0.3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    animation: chipIn 150ms ease;
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
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    display: inline-flex;
    align-items: baseline;
    gap: var(--spacing-xs);
  }

  .option-sub {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .actions-row {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-xs);
    border-top: 1px solid var(--border-subtle);
    margin-top: var(--spacing-xs);
  }

  /* ---- Right panel ---- */

  .install-right {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: 0;
  }

  .help-card {
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .help-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--accent-primary);
    margin-bottom: var(--spacing-sm);
  }

  .help-steps {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .help-steps li {
    display: grid;
    grid-template-columns: 24px 1fr;
    gap: var(--spacing-sm);
    align-items: start;
  }

  .step-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: rgba(var(--accent-primary-rgb, 108, 159, 255), 0.18);
    color: var(--accent-primary);
    font-size: 11px;
    font-weight: 700;
    font-family: var(--font-mono);
  }

  .step-head {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 2px;
  }

  .step-body {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .step-body a {
    color: var(--accent-primary);
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .step-body code {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-primary);
    background: var(--bg-elevated);
    padding: 1px 4px;
    border-radius: 3px;
  }

  /* ---- Installing stage ---- */

  .installing-panel {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-xl) var(--spacing-lg);
    text-align: center;
    min-height: 320px;
    justify-content: center;
  }

  .installing-spinner {
    position: relative;
    width: 48px;
    height: 48px;
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

  .installing-panel h3 {
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
    transition: color var(--transition-fast);
  }

  .progress-list li.active {
    color: var(--text-primary);
  }

  .progress-list li.done {
    color: var(--accent-primary);
  }

  .dot-idle {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--border-default);
    flex-shrink: 0;
  }

  .dot-pulse {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent-primary);
    box-shadow: 0 0 0 0 var(--accent-primary);
    animation: pulse 1.4s ease-out infinite;
    flex-shrink: 0;
  }

  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(var(--accent-primary-rgb, 108, 159, 255), 0.5); }
    70%  { box-shadow: 0 0 0 8px rgba(var(--accent-primary-rgb, 108, 159, 255), 0); }
    100% { box-shadow: 0 0 0 0 rgba(var(--accent-primary-rgb, 108, 159, 255), 0); }
  }

  .installing-note {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0;
    max-width: 360px;
  }

  .installing-note code {
    font-family: var(--font-mono);
    background: var(--bg-elevated);
    padding: 1px 5px;
    border-radius: 3px;
  }

  /* ---- Success stage ---- */

  .success-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    width: min(640px, 90vw);
  }

  .success-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
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

  .success-heading h3 {
    margin: 0 0 4px 0;
    font-size: var(--font-size-lg);
    color: var(--text-primary);
  }

  .success-sub {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .stat-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--spacing-sm);
  }

  @media (max-width: 560px) {
    .stat-row {
      grid-template-columns: 1fr;
    }
  }

  .stat {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .stat-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
  }

  .stat-value {
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .stat code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    word-break: break-all;
  }

  .tools-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .tools-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }

  .tools-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
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
    font-size: 11px;
    padding: 4px var(--spacing-sm);
    background: var(--bg-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    word-break: break-all;
  }

  .no-tools-note {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
    border: 1px solid rgba(245, 158, 11, 0.3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .success-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }
</style>
