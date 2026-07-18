<script lang="ts">
  import { onMount } from 'svelte';
  import type { AccountIdentity } from '$lib/types';
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { probeConnection } from '$lib/services/api.svelte';
  import { exchangePastedToken } from '$lib/utils/tokenHandoff';
  import { versionSkewNote } from '$lib/utils/versionSkew';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Avatar from '$lib/components/account/Avatar.svelte';
  import RoleChip from '$lib/components/account/RoleChip.svelte';
  import { identityDisplayName } from '$lib/components/account/avatar';

  interface Props {
    /** Fired after the verified connection is adopted into configStore. */
    onConnected: () => void | Promise<void>;
  }

  let { onConnected }: Props = $props();

  let apiUrl = $state(
    configStore.apiUrl || import.meta.env.VITE_DEFAULT_API_URL || 'http://localhost:8000'
  );
  let apiKey = $state('');

  type Status = 'idle' | 'testing' | 'verified' | 'error' | 'adopting';
  let status = $state<Status>('idle');
  let message = $state('');
  // Identity resolved by the probe, shown so the user can confirm the account
  // BEFORE anything is persisted.
  let resolvedIdentity = $state<AccountIdentity | null>(null);
  let versionNote = $state<string | null>(null);

  const connected = $derived(configStore.isConfigured && !!configStore.identity);

  onMount(() => {
    // Web-served UI: prefill the URL from the origin serving this page.
    const initialApiUrl = apiUrl;
    void configStore.autoDetectBackendOrigin().then((detected) => {
      if (detected && apiUrl === initialApiUrl) {
        apiUrl = detected;
      }
    });
  });

  function resetVerification() {
    if (status === 'verified' || status === 'error') {
      status = 'idle';
      message = '';
      resolvedIdentity = null;
      versionNote = null;
    }
  }

  // Probe is stateless and touches nothing in configStore, so a failed or
  // abandoned attempt leaves the app exactly as it was.
  async function verify() {
    status = 'testing';
    message = '';
    resolvedIdentity = null;
    versionNote = null;

    const result = await probeConnection(apiUrl, apiKey);
    if (!result.ok) {
      status = 'error';
      message = result.message;
      return;
    }

    resolvedIdentity = result.identity;
    status = 'verified';
    message = 'Connection verified.';
    versionNote = await versionSkewNote(result.backendVersion);
  }

  // Adopt the verified connection: exchange a pasted bootstrap token for a
  // long-lived personal one (the exchange silently keeps the pasted token on
  // failure), persist, and resolve identity so scoped stores read the right
  // keys. This flips needsSetup, but the surface stays mounted via the
  // onboarding session flag.
  async function adopt() {
    if (status !== 'verified') return;
    status = 'adopting';
    const label =
      typeof window !== 'undefined' && '__TAURI__' in window ? 'desktop-signin' : 'web-signin';
    const adoptedKey = await exchangePastedToken(apiUrl, apiKey, label);
    configStore.apiUrl = apiUrl;
    configStore.apiKey = adoptedKey;
    configStore.completeSetup();
    const identity = await configStore.refreshIdentity();
    if (identity) {
      connectionsStore.upsertAccountCredential({
        apiUrl,
        apiKey: adoptedKey,
        identity,
        makeActive: true,
      });
    }
    status = 'idle';
    apiKey = '';
    await onConnected();
  }
</script>

{#if connected && configStore.identity}
  <div class="connected-card">
    <Avatar identity={configStore.identity} size={44} state="connected" />
    <div class="connected-meta">
      <div class="connected-line">
        <strong>{identityDisplayName(configStore.identity)}</strong>
        <RoleChip role={configStore.identity.role} size="xs" />
      </div>
      <span class="connected-detail">Connected to {configStore.apiUrl}</span>
    </div>
    <span class="connected-badge">
      <Icon name="success" size={15} />
      Connected
    </span>
  </div>
  <p class="sf-hint">
    To switch servers or accounts later, use Settings, then Backend. The rest of
    this setup talks to this backend.
  </p>
{:else}
  <div class="sf-field">
    <label class="sf-label" for="setup-api-url">Backend URL</label>
    <input
      id="setup-api-url"
      class="sf-input"
      type="text"
      bind:value={apiUrl}
      oninput={resetVerification}
      placeholder="http://localhost:8000"
      autocomplete="off"
    />
    <p class="sf-hint">
      Where your NymeriaOS backend is running. http://localhost:8000 for a local install.
    </p>
  </div>

  <div class="sf-field">
    <label class="sf-label" for="setup-api-key">Account token</label>
    <input
      id="setup-api-key"
      class="sf-input"
      type="password"
      bind:value={apiKey}
      oninput={resetVerification}
      placeholder="nym_..."
      autocomplete="off"
    />
    <p class="sf-hint">
      On first run, use the bootstrap admin token from
      <code>&lt;data_dir&gt;/BOOTSTRAP_TOKEN.txt</code>. Bootstrap tokens are upgraded to a
      long-lived personal token automatically when you connect.
    </p>
  </div>

  <div class="sf-actions">
    <Button
      variant="secondary"
      onclick={verify}
      disabled={status === 'testing' || status === 'adopting' || !apiUrl.trim() || !apiKey.trim()}
      loading={status === 'testing'}
    >
      {status === 'testing' ? 'Verifying' : 'Verify connection'}
    </Button>
    <Button
      variant="primary"
      onclick={adopt}
      disabled={status !== 'verified' && status !== 'adopting'}
      loading={status === 'adopting'}
    >
      {status === 'adopting' ? 'Connecting' : 'Use this connection'}
    </Button>
  </div>

  {#if message}
    <div class="sf-result" class:success={status === 'verified'} class:error={status === 'error'}>
      <Icon name={status === 'error' ? 'error' : 'success'} size={16} />
      <span>{message}</span>
    </div>
  {/if}

  {#if resolvedIdentity}
    <div class="identity-preview">
      <Avatar identity={resolvedIdentity} size={40} state="connected" />
      <div class="connected-meta">
        <div class="connected-line">
          <span>You will be signed in as</span>
          <strong>{identityDisplayName(resolvedIdentity)}</strong>
          <RoleChip role={resolvedIdentity.role} size="xs" />
        </div>
        {#if resolvedIdentity.email && resolvedIdentity.email !== resolvedIdentity.display_name}
          <span class="connected-detail">{resolvedIdentity.email}</span>
        {/if}
      </div>
    </div>
  {/if}

  {#if versionNote}
    <p class="sf-hint">{versionNote}</p>
  {/if}
{/if}

<style>
  .connected-card {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm-plus);
    padding: var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
  }

  .connected-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .connected-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .connected-line strong {
    color: var(--text-primary);
  }

  .connected-detail {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .connected-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--success);
    flex-shrink: 0;
  }

  .identity-preview {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm-plus);
    padding: var(--spacing-sm-plus) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-md);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }
</style>
