<script lang="ts">
  import {
    CLIPROXY_CLAUDE_MODEL,
    CLIPROXY_CODEX_MODEL,
    LOCAL_CLIPROXY_ROOT_URL,
    LOCAL_OPENAI_CLIPROXY_BASE_URL,
    cliproxyStore,
  } from '$lib/stores/cliproxy.svelte';
  import { onDestroy, onMount } from 'svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';

  const claudeLoginCommand = 'docker exec -it cli-proxy-api-latest ./CLIProxyAPI --claude-login --no-browser';
  const codexLoginCommand = 'docker exec -it cli-proxy-api-latest ./CLIProxyAPI --codex-device-login --no-browser';
  const claudeSmokeCommand = `python3 Nymeria/tools/check_cliproxy_cloak.py --base-url ${LOCAL_CLIPROXY_ROOT_URL} --api-key cpx-... --auth-dir CLIProxyAPI-main/temp/latest/auths`;

  let copiedCommand = $state<string | null>(null);

  onMount(() => {
    cliproxyStore.startPolling();
  });

  onDestroy(() => {
    cliproxyStore.stopPolling();
  });

  async function copyCommand(key: string, command: string) {
    copiedCommand = null;
    await navigator.clipboard.writeText(command);
    copiedCommand = key;
    window.setTimeout(() => {
      if (copiedCommand === key) copiedCommand = null;
    }, 1800);
  }
</script>

<div class="tab-content">
  <div class="field">
    <span class="field-label">CLIProxy Status</span>
    <div class="status-row">
      <span class="status-dot" class:running={cliproxyStore.running} class:stopped={!cliproxyStore.running}></span>
      <span class="status-text">
        {#if cliproxyStore.running}
          Reachable at {cliproxyStore.baseUrl}
        {:else}
          Not reachable
        {/if}
      </span>
    </div>
    {#if cliproxyStore.detail}
      <p class="hint">{cliproxyStore.detail}</p>
    {/if}
    <p class="hint">Source-checkout controls for the pinned Docker deployment at <code>CLIProxyAPI-main/temp/latest</code>.</p>
  </div>

  <div class="field">
    <div class="actions">
      {#if cliproxyStore.running}
        <Button variant="secondary" onclick={cliproxyStore.stop} disabled={cliproxyStore.loading}>
          <Icon name="stop" size={14} />
          {cliproxyStore.loading ? 'Stopping' : 'Stop CLIProxy'}
        </Button>
      {:else}
        <Button variant="primary" onclick={cliproxyStore.start} disabled={cliproxyStore.loading}>
          <Icon name="server" size={14} />
          {cliproxyStore.loading ? 'Starting' : 'Start CLIProxy'}
        </Button>
      {/if}
      <Button variant="secondary" onclick={cliproxyStore.refreshStatus} disabled={cliproxyStore.loading}>
        <Icon name="refresh" size={14} />
        Refresh
      </Button>
    </div>
  </div>

  {#if cliproxyStore.running}
    <div class="route-grid">
      <section class="route-panel">
        <div class="route-heading">
          <Icon name="server" size={18} />
          <div>
            <span class="route-title">Claude / Anthropic OAuth</span>
            <span class="route-subtitle">Root URL, no /v1 suffix</span>
          </div>
        </div>

        <div class="route-facts">
          <div>
            <span>Provider</span>
            <strong>Anthropic</strong>
          </div>
          <div>
            <span>Model</span>
            <strong>{CLIPROXY_CLAUDE_MODEL}</strong>
          </div>
          <div class="wide">
            <span>Base URL</span>
            <code>{LOCAL_CLIPROXY_ROOT_URL}</code>
          </div>
        </div>

        <div class="sessions-list">
          {#if cliproxyStore.claudeSessions.length > 0}
            {#each cliproxyStore.claudeSessions as session}
              <div class="session-item">
                <span class="session-provider">{session.provider}</span>
                {#if session.email}
                  <span class="session-email">{session.email}</span>
                {/if}
              </div>
            {/each}
          {:else}
            <p class="hint warning">No Claude OAuth session reported.</p>
          {/if}
        </div>

        <div class="route-actions">
          <Button variant="secondary" onclick={() => copyCommand('claude-login', claudeLoginCommand)}>
            <Icon name="copy" size={14} />
            {copiedCommand === 'claude-login' ? 'Copied' : 'Copy Login Command'}
          </Button>
          <Button variant="secondary" onclick={() => copyCommand('claude-smoke', claudeSmokeCommand)}>
            <Icon name="copy" size={14} />
            {copiedCommand === 'claude-smoke' ? 'Copied' : 'Copy Smoke Test'}
          </Button>
          <Button variant="primary" onclick={cliproxyStore.applyClaudeRoute}>
            <Icon name="check" size={14} />
            Apply Anthropic Route
          </Button>
        </div>
      </section>

      <section class="route-panel">
        <div class="route-heading">
          <Icon name="terminal" size={18} />
          <div>
            <span class="route-title">Codex / OpenAI OAuth</span>
            <span class="route-subtitle">OpenAI-compatible /v1 URL, Responses API</span>
          </div>
        </div>

        <div class="route-facts">
          <div>
            <span>Provider</span>
            <strong>OpenAI</strong>
          </div>
          <div>
            <span>Model</span>
            <strong>{CLIPROXY_CODEX_MODEL}</strong>
          </div>
          <div>
            <span>API Mode</span>
            <strong>Responses</strong>
          </div>
          <div class="wide">
            <span>Base URL</span>
            <code>{LOCAL_OPENAI_CLIPROXY_BASE_URL}</code>
          </div>
        </div>

        <div class="sessions-list">
          {#if cliproxyStore.openAISessions.length > 0}
            {#each cliproxyStore.openAISessions as session}
              <div class="session-item">
                <span class="session-provider">{session.provider}</span>
                {#if session.email}
                  <span class="session-email">{session.email}</span>
                {/if}
              </div>
            {/each}
          {:else}
            <p class="hint warning">No Codex/OpenAI OAuth session reported.</p>
          {/if}
        </div>

        <div class="route-actions">
          <Button variant="secondary" onclick={() => copyCommand('codex-login', codexLoginCommand)}>
            <Icon name="copy" size={14} />
            {copiedCommand === 'codex-login' ? 'Copied' : 'Copy Login Command'}
          </Button>
          <Button variant="primary" onclick={cliproxyStore.applyCodexRoute}>
            <Icon name="check" size={14} />
            Apply OpenAI Route
          </Button>
        </div>
      </section>
    </div>

    <div class="field">
      <span class="field-label">Route Reset</span>
      <div class="actions">
        <Button variant="secondary" onclick={cliproxyStore.useDirectApi}>
          <Icon name="x" size={14} />
          Clear Base URL
        </Button>
      </div>
      <p class="hint">Route buttons update provider settings only. Configure the matching <code>cpx-...</code> gatekeeper key in Provider Setup or the backend env file.</p>
    </div>
  {/if}

  {#if cliproxyStore.message}
    <div class="message success">
      <Icon name="success" size={16} />
      <span>{cliproxyStore.message}</span>
    </div>
  {/if}

  {#if cliproxyStore.error}
    <div class="message error">
      <Icon name="error" size={16} />
      <span>{cliproxyStore.error}</span>
    </div>
  {/if}
</div>

<style>
  .tab-content {
    padding: 1rem 0;
  }

  .field {
    margin-bottom: 1.25rem;
  }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    /* §4 — labels recede behind the input value (which is --text-primary),
       so the eye finds the answer before the question. */
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0.25rem 0 0 0;
    line-height: 1.45;
  }

  .hint code,
  .route-facts code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow-wrap: anywhere;
  }

  .hint.warning {
    color: var(--warning);
  }

  .status-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-dot.running {
    background: var(--success);
    box-shadow: 0 0 6px var(--success);
  }

  .status-dot.stopped {
    background: var(--text-muted);
  }

  .status-text {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .actions,
  .route-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .route-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.25rem;
  }

  .route-panel {
    display: flex;
    flex-direction: column;
    gap: 0.875rem;
    min-width: 0;
    padding: 0.875rem;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: 6px;
  }

  .route-heading {
    display: flex;
    align-items: flex-start;
    gap: 0.625rem;
    min-width: 0;
  }

  .route-heading > div {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
  }

  .route-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .route-subtitle {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .route-facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.5rem;
  }

  .route-facts div {
    min-width: 0;
    padding: 0.5rem;
    border-radius: 6px;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
  }

  .route-facts .wide {
    grid-column: 1 / -1;
  }

  .route-facts span {
    display: block;
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
    margin-bottom: 0.125rem;
  }

  .route-facts strong {
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .sessions-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    min-height: 2rem;
  }

  .session-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: var(--bg-elevated);
    border-radius: 6px;
    border: 1px solid var(--border-subtle);
  }

  .session-provider {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .session-email {
    min-width: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .message {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    border-radius: 6px;
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .message.success {
    background: rgba(var(--success-rgb), 0.12);
    border: 1px solid rgba(var(--success-rgb), 0.35);
    color: var(--success);
  }

  .message.error {
    background: rgba(var(--error-rgb), 0.1);
    border: 1px solid var(--error);
    color: var(--error);
  }

  @media (max-width: 840px) {
    .route-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
