<script lang="ts">
  import { cliproxyStore } from '$lib/stores/cliproxy.svelte';
  import type { CLIProxyProviderInfo } from '$lib/types';
  import { onDestroy, onMount } from 'svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';

  const TOS_DISCLAIMER =
    'Subscription OAuth routes a personal AI subscription through CLIProxy instead of a pay-per-token API key. '
    + 'Providers generally consider third-party use of their subscription clients a terms-of-service violation and '
    + 'may rate-limit or suspend the account. Use a dedicated account if that risk matters to you.';

  let tosAccepted = $state(false);
  let callbackUrl = $state('');
  let modelByProvider = $state<Record<string, string>>({});
  let requestRetry = $state<string>('');
  let routingStrategy = $state<string>('');
  let knobsLoaded = $state(false);

  onMount(async () => {
    await cliproxyStore.refresh();
    if (cliproxyStore.reachable) {
      await cliproxyStore.loadKnobs();
      syncKnobInputs();
      knobsLoaded = true;
    }
  });

  onDestroy(() => {
    cliproxyStore.dismissOAuth();
  });

  function syncKnobInputs() {
    const knobs = cliproxyStore.knobs;
    requestRetry = knobs['request-retry'] != null ? String(knobs['request-retry']) : '';
    routingStrategy = knobs['routing/strategy'] != null ? String(knobs['routing/strategy']) : '';
  }

  function modelFor(provider: CLIProxyProviderInfo): string {
    return modelByProvider[provider.id] ?? provider.default_model;
  }

  async function login(provider: CLIProxyProviderInfo) {
    // Logging in is the action that actually exposes the subscription
    // account, so the TOS acknowledgment gates it for every provider.
    if (!tosAccepted) return;
    callbackUrl = '';
    await cliproxyStore.startOAuth(provider);
  }

  async function deliver() {
    const pasted = callbackUrl.trim();
    if (!pasted) return;
    await cliproxyStore.deliverCallback(pasted);
    callbackUrl = '';
  }

  async function applyGlobal(provider: CLIProxyProviderInfo) {
    await cliproxyStore.applyRoute(provider.id, { model: modelFor(provider).trim() });
  }

  async function saveKnobs() {
    const update: Record<string, unknown> = {};
    const retry = Number.parseInt(requestRetry, 10);
    if (!Number.isNaN(retry)) update['request-retry'] = retry;
    if (routingStrategy.trim()) update['routing/strategy'] = routingStrategy.trim();
    if (Object.keys(update).length === 0) return;
    await cliproxyStore.saveKnobs(update);
    syncKnobInputs();
  }
</script>

<div class="tab-content">
  <div class="field">
    <span class="field-label">CLIProxy</span>
    <div class="status-row">
      <span
        class="status-dot"
        class:running={cliproxyStore.reachable}
        class:stopped={!cliproxyStore.reachable}
      ></span>
      <span class="status-text">
        {#if !cliproxyStore.status}
          Couldn't load the CLIProxy status from the backend; check the connection and refresh
        {:else if !cliproxyStore.configured}
          Not configured on the backend (set CLIPROXY_MANAGEMENT_URL and CLIPROXY_MANAGEMENT_KEY, or run nymeria init)
        {:else if cliproxyStore.reachable}
          Management API reachable
        {:else}
          Configured but unreachable{cliproxyStore.status?.detail ? `: ${cliproxyStore.status.detail}` : ''}
        {/if}
      </span>
    </div>
    <div class="actions">
      <Button variant="secondary" onclick={() => cliproxyStore.refresh(true)}>
        <Icon name="refresh" size={14} />
        Refresh
      </Button>
      {#if cliproxyStore.managementHtmlUrl}
        <a class="panel-link" href={cliproxyStore.managementHtmlUrl} target="_blank" rel="noopener">
          Open the proxy's own panel for advanced settings
        </a>
      {/if}
    </div>
  </div>

  {#if cliproxyStore.reachable}
    <div class="field tos">
      <label class="tos-label">
        <input type="checkbox" bind:checked={tosAccepted} />
        <span>{TOS_DISCLAIMER} I understand.</span>
      </label>
    </div>

    {#if cliproxyStore.oauth}
      {@const flow = cliproxyStore.oauth}
      <div class="oauth-box" class:ok={flow.status === 'ok'} class:err={flow.status === 'error'}>
        <div class="oauth-head">
          <strong>Login: {flow.provider}</strong>
          <Button variant="secondary" onclick={cliproxyStore.dismissOAuth}>
            <Icon name="x" size={12} />
            Dismiss
          </Button>
        </div>
        <p class="hint">{flow.detail}</p>
        <a class="panel-link" href={flow.url} target="_blank" rel="noopener">Open the login page again</a>
        {#if flow.flow === 'browser' && flow.status !== 'ok'}
          <div class="callback-row">
            <input
              class="text-input"
              type="text"
              placeholder="Paste the full redirect URL (http://localhost:.../callback?code=...)"
              bind:value={callbackUrl}
            />
            <Button variant="primary" onclick={deliver} disabled={!callbackUrl.trim()}>
              Deliver
            </Button>
          </div>
        {/if}
      </div>
    {/if}

    <div class="provider-grid">
      {#each cliproxyStore.providers as provider (provider.id)}
        {@const files = cliproxyStore.authFilesFor(provider.id)}
        <section class="provider-card" class:unsupported={provider.supported === false}>
          <div class="provider-head">
            <div>
              <span class="provider-title">{provider.label}</span>
              <span class="provider-subtitle">{provider.description}</span>
            </div>
            {#if provider.supported === false}
              <span class="badge muted">Not supported by this proxy build</span>
            {:else if provider.logged_in}
              <span class="badge ok">Logged in</span>
            {:else}
              <span class="badge warn">No login</span>
            {/if}
          </div>

          {#if provider.tos_warning}
            <p class="hint warning">{provider.tos_warning}</p>
          {/if}

          {#if files.length > 0}
            <div class="sessions-list">
              {#each files as file (file.name)}
                <div class="session-item">
                  <span class="session-email">{file.account || file.email || file.name}</span>
                  <span class="session-state">{file.disabled ? 'disabled' : file.status || 'active'}</span>
                  <div class="session-actions">
                    <Button
                      variant="secondary"
                      onclick={() => cliproxyStore.setAuthFileDisabled(file.name, !file.disabled)}
                    >
                      {file.disabled ? 'Enable' : 'Disable'}
                    </Button>
                    <Button variant="secondary" onclick={() => cliproxyStore.deleteAuthFile(file.name)}>
                      Remove
                    </Button>
                  </div>
                </div>
              {/each}
            </div>
          {/if}

          <div class="route-row">
            <input
              class="text-input"
              type="text"
              placeholder={provider.default_model}
              value={modelFor(provider)}
              oninput={(event) => {
                modelByProvider = {
                  ...modelByProvider,
                  [provider.id]: (event.currentTarget as HTMLInputElement).value
                };
              }}
            />
          </div>

          <div class="route-actions">
            <Button
              variant="secondary"
              onclick={() => login(provider)}
              disabled={provider.supported === false || !tosAccepted}
            >
              <Icon name="key" size={14} />
              {provider.logged_in ? 'Re-login' : 'Log in'}
            </Button>
            <Button
              variant="primary"
              onclick={() => applyGlobal(provider)}
              disabled={provider.supported === false || !provider.logged_in}
            >
              <Icon name="check" size={14} />
              Use for all chats
            </Button>
          </div>
        </section>
      {/each}
    </div>

    {#if knobsLoaded}
      <div class="field">
        <span class="field-label">Proxy settings</span>
        <div class="knob-row">
          <label class="knob">
            <span>Request retries</span>
            <input class="text-input narrow" type="number" min="0" bind:value={requestRetry} />
          </label>
          <label class="knob">
            <span>Routing strategy</span>
            <input class="text-input" type="text" placeholder="round-robin" bind:value={routingStrategy} />
          </label>
          <Button variant="secondary" onclick={saveKnobs}>Save</Button>
        </div>
        <p class="hint">
          Model aliases, excluded models, quota behavior, and gatekeeper keys live in the proxy's own panel
          (link above).
        </p>
      </div>
    {/if}
  {/if}

  {#if cliproxyStore.localRunning || cliproxyStore.localSessions.length > 0}
    <div class="field">
      <span class="field-label">Local container (this machine)</span>
      <p class="hint">A desktop-managed CLIProxy container is {cliproxyStore.localRunning ? 'running' : 'stopped'}.</p>
      <div class="actions">
        {#if cliproxyStore.localRunning}
          <Button variant="secondary" onclick={cliproxyStore.stopLocal} disabled={cliproxyStore.loading}>
            <Icon name="stop" size={14} />
            Stop local container
          </Button>
        {:else}
          <Button variant="secondary" onclick={cliproxyStore.startLocal} disabled={cliproxyStore.loading}>
            <Icon name="server" size={14} />
            Start local container
          </Button>
        {/if}
      </div>
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
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0.25rem 0 0 0;
    line-height: 1.45;
  }

  .hint.warning {
    color: var(--warning);
  }

  .status-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
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

  .actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .panel-link {
    font-size: var(--font-size-xs);
    color: var(--accent);
  }

  .tos-label {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.45;
    cursor: pointer;
  }

  .tos-label input {
    margin-top: 0.15rem;
  }

  .oauth-box {
    padding: 0.875rem;
    border-radius: 6px;
    border: 1px solid var(--border-default);
    background: var(--bg-elevated-2);
    margin-bottom: 1rem;
  }

  .oauth-box.ok {
    border-color: var(--success);
  }

  .oauth-box.err {
    border-color: var(--error);
  }

  .oauth-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .callback-row {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .text-input {
    flex: 1;
    min-width: 0;
    padding: 0.45rem 0.6rem;
    border-radius: 6px;
    border: 1px solid var(--border-default);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .text-input.narrow {
    flex: 0 0 5rem;
  }

  .provider-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.25rem;
  }

  .provider-card {
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
    min-width: 0;
    padding: 0.875rem;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: 6px;
  }

  .provider-card.unsupported {
    opacity: 0.6;
  }

  .provider-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .provider-head > div {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
  }

  .provider-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .provider-subtitle {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .badge {
    flex-shrink: 0;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-size: var(--font-size-2xs);
    font-weight: 600;
    white-space: nowrap;
  }

  .badge.ok {
    background: rgba(var(--success-rgb), 0.15);
    color: var(--success);
  }

  .badge.warn {
    background: rgba(var(--warning-rgb), 0.15);
    color: var(--warning);
  }

  .badge.muted {
    background: var(--bg-elevated);
    color: var(--text-muted);
  }

  .sessions-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .session-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: var(--bg-elevated);
    border-radius: 6px;
    border: 1px solid var(--border-subtle);
  }

  .session-email {
    flex: 1;
    min-width: 0;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .session-state {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .session-actions {
    display: flex;
    gap: 0.375rem;
  }

  .route-row {
    display: flex;
    gap: 0.5rem;
  }

  .route-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .knob-row {
    display: flex;
    align-items: flex-end;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .knob {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
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
    .provider-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
