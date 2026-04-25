<script lang="ts">
  import type { TokenInfo } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import { configStore } from '$lib/stores/config.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import CopyOnceTokenDialog from './CopyOnceTokenDialog.svelte';

  interface Props {
    /**
     * Whose tokens to manage. Phase C only supports 'self' (calls /me/tokens
     * with the current Bearer). Phase D will add 'admin' (calls
     * /admin/users/{id}/tokens) by accepting a userId here.
     */
    mode?: 'self';
  }

  let { mode = 'self' }: Props = $props();

  let tokens = $state<TokenInfo[]>([]);
  let loading = $state(false);
  let loadError = $state<string | null>(null);

  let showIssueDialog = $state(false);
  let issueLabel = $state('');
  let issuing = $state(false);
  let issueError = $state<string | null>(null);

  let showCopyDialog = $state(false);
  let issuedRawToken = $state<string | null>(null);
  let issuedLabel = $state<string | null>(null);

  let revokingPrefix = $state<string | null>(null);

  async function load() {
    if (loading) return;
    loading = true;
    loadError = null;
    try {
      tokens = await api.listMyTokens();
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load tokens';
    } finally {
      loading = false;
    }
  }

  // Initial load + reload whenever the calling identity changes (e.g. switched
  // accounts in the AccountSwitcher).
  $effect(() => {
    void configStore.identity?.id; // tracked dependency
    void load();
  });

  function openIssueDialog() {
    issueLabel = '';
    issueError = null;
    showIssueDialog = true;
  }

  function closeIssueDialog() {
    if (issuing) return;
    showIssueDialog = false;
  }

  async function handleIssue() {
    if (issuing) return;
    issueError = null;
    issuing = true;
    try {
      const res = await api.issueMyToken(issueLabel.trim() || undefined);
      issuedRawToken = res.raw_token;
      issuedLabel = res.metadata.label;
      showIssueDialog = false;
      showCopyDialog = true;
      // Refresh list to show the new entry.
      await load();
    } catch (e) {
      issueError = e instanceof Error ? e.message : 'Failed to issue token';
    } finally {
      issuing = false;
    }
  }

  function closeCopyDialog() {
    showCopyDialog = false;
    issuedRawToken = null;
    issuedLabel = null;
  }

  async function handleRevoke(token: TokenInfo) {
    if (revokingPrefix) return;
    const labelPart = token.label ? ` "${token.label}"` : '';
    const ok = window.confirm(
      `Revoke token${labelPart} (${token.token_hash_prefix})? Any client using it will be signed out immediately.`
    );
    if (!ok) return;
    revokingPrefix = token.token_hash_prefix;
    try {
      await api.revokeMyToken(token.token_hash_prefix);
      await load();
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to revoke token';
    } finally {
      revokingPrefix = null;
    }
  }

  function formatRelative(iso: string | null): string {
    if (!iso) return '—';
    try {
      const ms = Date.now() - new Date(iso).getTime();
      if (ms < 60_000) return 'just now';
      if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
      if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
      const days = Math.floor(ms / 86_400_000);
      if (days < 30) return `${days}d ago`;
      return new Date(iso).toLocaleDateString();
    } catch {
      return iso;
    }
  }

  let activeTokens = $derived(tokens.filter((t) => !t.revoked_at));
  let revokedTokens = $derived(tokens.filter((t) => t.revoked_at));
</script>

<div class="token-section">
  <div class="header">
    <div class="header-meta">
      <span class="active-count">
        {activeTokens.length} active token{activeTokens.length === 1 ? '' : 's'}
      </span>
      {#if revokedTokens.length > 0}
        <span class="revoked-count">· {revokedTokens.length} revoked</span>
      {/if}
    </div>
    <Button size="sm" onclick={openIssueDialog} disabled={loading}>
      <Icon name="plus" size={14} />
      Issue token
    </Button>
  </div>

  {#if loadError}
    <div class="error-banner">
      <Icon name="error" size={14} />
      <span>{loadError}</span>
      <button class="banner-action" type="button" onclick={load}>Retry</button>
    </div>
  {/if}

  {#if loading && tokens.length === 0}
    <div class="empty-state">
      <Icon name="loading" size={20} />
      <span>Loading tokens…</span>
    </div>
  {:else if tokens.length === 0}
    <div class="empty-state">
      <Icon name="info" size={20} />
      <span>No tokens yet. Issue one to use Nymeria from another browser, the mobile app, or the API.</span>
    </div>
  {:else}
    <div class="token-list">
      {#each tokens as token (token.token_hash_prefix)}
        {@const isRevoked = !!token.revoked_at}
        {@const isRevoking = revokingPrefix === token.token_hash_prefix}
        <div class="token-row" class:revoked={isRevoked}>
          <div class="row-meta">
            <div class="row-line">
              <code class="prefix">nym_{token.token_hash_prefix}…</code>
              {#if token.label}
                <span class="label">{token.label}</span>
              {/if}
              {#if isRevoked}
                <span class="status-chip revoked">Revoked</span>
              {/if}
            </div>
            <div class="row-secondary">
              <span title={token.created_at}>Issued {formatRelative(token.created_at)}</span>
              <span class="dot" aria-hidden="true">·</span>
              <span title={token.last_used_at ?? 'never used'}>
                {token.last_used_at ? `Last used ${formatRelative(token.last_used_at)}` : 'Never used'}
              </span>
              {#if isRevoked}
                <span class="dot" aria-hidden="true">·</span>
                <span title={token.revoked_at}>Revoked {formatRelative(token.revoked_at)}</span>
              {/if}
            </div>
          </div>
          {#if !isRevoked}
            <button
              class="revoke-btn"
              type="button"
              onclick={() => handleRevoke(token)}
              disabled={isRevoking}
              title="Revoke this token"
              aria-label="Revoke token {token.token_hash_prefix}"
            >
              {#if isRevoking}
                <Icon name="loading" size={14} />
              {:else}
                <Icon name="trash" size={14} />
              {/if}
            </button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<!-- Issue label modal -->
<Modal title="Issue new token" isOpen={showIssueDialog} onClose={closeIssueDialog}>
  <div class="issue-form">
    <p class="issue-intro">
      Optionally label this token so you can recognise it later (e.g.
      "iPhone", "work laptop", "discord-bot script").
    </p>
    <div class="field">
      <label for="issue-label">Label (optional)</label>
      <input
        id="issue-label"
        type="text"
        bind:value={issueLabel}
        placeholder="e.g. iPhone"
        disabled={issuing}
        maxlength="80"
        autocomplete="off"
      />
    </div>

    {#if issueError}
      <div class="form-error">
        <Icon name="error" size={14} />
        <span>{issueError}</span>
      </div>
    {/if}

    <div class="actions">
      <Button onclick={handleIssue} disabled={issuing}>
        {issuing ? 'Issuing…' : 'Issue token'}
      </Button>
      <Button variant="ghost" onclick={closeIssueDialog} disabled={issuing}>Cancel</Button>
    </div>
  </div>
</Modal>

<CopyOnceTokenDialog
  isOpen={showCopyDialog}
  onClose={closeCopyDialog}
  rawToken={issuedRawToken}
  label={issuedLabel}
/>

<style>
  .token-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .header-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .active-count {
    color: var(--text-primary);
    font-weight: 500;
  }

  .revoked-count {
    color: var(--text-muted);
  }

  .error-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px var(--spacing-sm);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: var(--radius-sm);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .banner-action {
    margin-left: auto;
    color: var(--error);
    background: transparent;
    border: 1px solid currentColor;
    border-radius: var(--radius-sm);
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
  }

  .empty-state {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: var(--spacing-md);
    color: var(--text-muted);
    background: var(--bg-elevated);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .token-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .token-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 10px var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    transition: border-color var(--transition-fast);
  }

  .token-row:hover {
    border-color: var(--border-default);
  }

  .token-row.revoked {
    opacity: 0.65;
  }

  .row-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .row-line {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;
  }

  .prefix {
    font-family: var(--font-mono, ui-monospace, 'SF Mono', monospace);
    font-size: 12px;
    color: var(--text-secondary);
    background: var(--bg-base);
    padding: 1px 6px;
    border-radius: 3px;
  }

  .label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .status-chip {
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 1px 6px;
    border-radius: 4px;
  }

  .status-chip.revoked {
    color: var(--error);
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.4);
  }

  .row-secondary {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--text-muted);
    flex-wrap: wrap;
  }

  .dot {
    color: var(--text-muted);
  }

  .revoke-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .revoke-btn:hover:not(:disabled) {
    color: var(--error);
    background: rgba(239, 68, 68, 0.1);
  }

  .revoke-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .issue-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(380px, 90vw);
  }

  .issue-intro {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .field input {
    padding: 8px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .field input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(34, 211, 238, 0.15));
  }

  .form-error {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }
</style>
