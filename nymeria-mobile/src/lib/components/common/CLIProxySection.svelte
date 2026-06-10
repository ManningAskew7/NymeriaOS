<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import type { CLIProxyProviderInfo, CLIProxyStatus } from '$lib/types';
  import { onDestroy, onMount } from 'svelte';
  import Button from './Button.svelte';

  /**
   * Essential CLIProxy parity for mobile: status, subscription logins, and
   * apply-route. The backend orchestrates the whole OAuth dance, so a phone
   * can complete it (open the URL, approve, paste the redirect if needed).
   * Knob editing and auth-file management stay on desktop or the proxy's own
   * panel.
   */

  const TOS_NOTE =
    'Subscription OAuth routes a personal AI subscription through CLIProxy. '
    + 'Providers generally treat this as a terms-of-service violation; use a '
    + 'dedicated account if that risk matters.';

  let status = $state<CLIProxyStatus | null>(null);
  let error = $state<string | null>(null);
  let message = $state<string | null>(null);
  let oauthProvider = $state<string | null>(null);
  let oauthUrl = $state('');
  let oauthState = $state('');
  let oauthDetail = $state('');
  let callbackUrl = $state('');
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  onMount(refresh);
  onDestroy(stopPolling);

  async function refresh() {
    status = await api.getCLIProxyStatus();
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function login(provider: CLIProxyProviderInfo) {
    error = null;
    message = null;
    stopPolling();
    try {
      const started = await api.startCLIProxyOAuth(provider.id);
      oauthProvider = provider.id;
      oauthUrl = started.url;
      oauthState = started.state;
      oauthDetail =
        started.flow === 'device'
          ? 'Open the link and approve the login; this screen updates by itself.'
          : 'Approve the login in the browser. If it ends on a dead localhost page, paste that page\'s full URL below.';
      window.open(started.url, '_blank', 'noopener');
      pollTimer = setInterval(async () => {
        try {
          const result = await api.getCLIProxyOAuthStatus(started.state, provider.id);
          if (result === 'ok') {
            stopPolling();
            oauthProvider = null;
            message = `${provider.label} login complete.`;
            await refresh();
          } else if (result === 'error') {
            stopPolling();
            oauthDetail = 'The provider reported a login error; start it again.';
          }
        } catch (e) {
          stopPolling();
          oauthDetail = humanizeErrorText(e, { action: 'connect', resource: 'the proxy' });
        }
      }, 2500);
    } catch (e) {
      error = humanizeErrorText(e, { action: 'start', resource: 'the login' });
    }
  }

  async function deliver() {
    const pasted = callbackUrl.trim();
    if (!pasted || !oauthProvider) return;
    try {
      await api.deliverCLIProxyOAuthCallback(oauthProvider, pasted);
      callbackUrl = '';
      oauthDetail = 'Callback delivered; finishing the login...';
    } catch (e) {
      oauthDetail = humanizeErrorText(e, { action: 'send', resource: 'the callback' });
    }
  }

  async function applyRoute(provider: CLIProxyProviderInfo) {
    error = null;
    message = null;
    try {
      const applied = await api.applyCLIProxyRoute({ provider: provider.id });
      message = `Backend route set to ${applied.provider} via CLIProxy (${applied.model}).`;
    } catch (e) {
      error = humanizeErrorText(e, { action: 'save', resource: 'the LLM route' });
    }
  }
</script>

{#if status?.configured}
  <div class="setting-group">
    <span class="setting-label">CLIProxy subscriptions</span>
    {#if !status.reachable}
      <p class="hint">The proxy is configured but unreachable{status.detail ? `: ${status.detail}` : ''}.</p>
    {:else}
      <p class="hint">{TOS_NOTE}</p>

      {#if oauthProvider}
        <div class="oauth-box">
          <p class="hint">{oauthDetail}</p>
          <a class="link" href={oauthUrl} target="_blank" rel="noopener">Open the login page again</a>
          <div class="callback-row">
            <input
              class="setting-input"
              type="text"
              placeholder="Paste the redirect URL here if needed"
              bind:value={callbackUrl}
            />
            <Button variant="primary" onclick={deliver} disabled={!callbackUrl.trim()}>Deliver</Button>
          </div>
        </div>
      {/if}

      <div class="provider-list">
        {#each status.providers as provider (provider.id)}
          <div class="provider-row" class:unsupported={provider.supported === false}>
            <div class="provider-info">
              <span class="provider-name">{provider.label}</span>
              <span class="provider-state">
                {#if provider.supported === false}
                  Not supported by this proxy build
                {:else if provider.logged_in}
                  Logged in
                {:else}
                  No login
                {/if}
              </span>
            </div>
            <div class="provider-actions">
              <Button
                variant="secondary"
                onclick={() => login(provider)}
                disabled={provider.supported === false}
              >
                {provider.logged_in ? 'Re-login' : 'Log in'}
              </Button>
              <Button
                variant="primary"
                onclick={() => applyRoute(provider)}
                disabled={provider.supported === false || !provider.logged_in}
              >
                Use
              </Button>
            </div>
          </div>
        {/each}
      </div>

      {#if status.management_html_url}
        <p class="hint">
          Advanced proxy settings: open <code>{status.management_html_url}</code> on a machine that can reach the proxy.
        </p>
      {/if}
    {/if}

    {#if message}
      <p class="result success">{message}</p>
    {/if}
    {#if error}
      <p class="result error">{error}</p>
    {/if}
  </div>
{/if}

<style>
  .setting-group {
    margin-bottom: 1.25rem;
  }

  .setting-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0.25rem 0;
    line-height: 1.45;
  }

  .hint code {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }

  .link {
    font-size: var(--font-size-xs);
    color: var(--accent);
  }

  .oauth-box {
    padding: 0.625rem;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--bg-elevated-2);
    margin: 0.5rem 0;
  }

  .callback-row {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .setting-input {
    flex: 1;
    min-width: 0;
    padding: 0.5rem 0.625rem;
    border-radius: 8px;
    border: 1px solid var(--border-default);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .provider-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .provider-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.625rem;
    border: 1px solid var(--border-subtle);
    border-radius: 8px;
    background: var(--bg-elevated);
  }

  .provider-row.unsupported {
    opacity: 0.55;
  }

  .provider-info {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
  }

  .provider-name {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .provider-state {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .provider-actions {
    display: flex;
    gap: 0.375rem;
    flex-shrink: 0;
  }

  .result {
    margin-top: 0.5rem;
    font-size: var(--font-size-xs);
    line-height: 1.4;
  }

  .result.success {
    color: var(--success);
  }

  .result.error {
    color: var(--error);
  }
</style>
