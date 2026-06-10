<script lang="ts">
  import { untrack } from 'svelte';
  import type { TokenInfo } from '$lib/types';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { configStore } from '$lib/stores/config.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import CopyOnceTokenDialog from './CopyOnceTokenDialog.svelte';

  interface Props {
    /**
     * Whose tokens to manage:
     *   - 'self'  (default): wraps /me/tokens; uses the calling user's bearer.
     *   - 'admin': wraps /admin/users/{userId}/tokens. Admin mode also adds
     *     a "Rotate all" action.
     */
    mode?: 'self' | 'admin';
    /** Required when mode === 'admin'. */
    userId?: string;
    userLabel?: string;
  }

  let { mode = 'self', userId, userLabel }: Props = $props();

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
  let rotating = $state(false);

  async function load() {
    if (loading) return;
    loading = true;
    loadError = null;
    try {
      if (mode === 'admin' && userId) {
        tokens = await api.listUserTokens(userId);
      } else {
        tokens = await api.listMyTokens();
      }
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'load', resource: 'your tokens' });
    } finally {
      loading = false;
    }
  }

  // load() reads `loading` to short-circuit re-entry; wrap in untrack so that
  // read doesn't re-fire this effect every time the in-flight fetch toggles
  // loading on/off.
  $effect(() => {
    void configStore.identity?.id;
    void userId;
    untrack(() => {
      void load();
    });
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
      const labelArg = issueLabel.trim() || undefined;
      const res =
        mode === 'admin' && userId
          ? await api.issueUserToken(userId, labelArg)
          : await api.issueMyToken(labelArg);
      issuedRawToken = res.raw_token;
      issuedLabel = res.metadata.label;
      showIssueDialog = false;
      showCopyDialog = true;
      await load();
    } catch (e) {
      issueError = humanizeErrorText(e, { action: 'create', resource: 'the token' });
    } finally {
      issuing = false;
    }
  }

  async function handleRotateAll() {
    if (!(mode === 'admin' && userId) || rotating) return;
    const subject = userLabel ? `for ${userLabel}` : 'for this user';
    const ok = window.confirm(
      `Rotate every active token ${subject}? All current sessions will be signed out and a fresh token will be issued.`
    );
    if (!ok) return;
    rotating = true;
    loadError = null;
    try {
      const res = await api.rotateUserTokens(userId);
      issuedRawToken = res.raw_token;
      issuedLabel = res.metadata.label;
      showCopyDialog = true;
      await load();
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'update', resource: 'your tokens' });
    } finally {
      rotating = false;
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
      if (mode === 'admin' && userId) {
        await api.revokeUserToken(userId, token.token_hash_prefix);
      } else {
        await api.revokeMyToken(token.token_hash_prefix);
      }
      await load();
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'delete', resource: 'the token' });
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
    <div class="header-actions">
      {#if mode === 'admin' && userId}
        <Button
          size="sm"
          variant="ghost"
          onclick={handleRotateAll}
          disabled={rotating || issuing || activeTokens.length === 0}
        >
          <Icon name={rotating ? 'loading' : 'refresh'} size={14} />
          {rotating ? 'Rotating…' : 'Rotate'}
        </Button>
      {/if}
      <Button size="sm" onclick={openIssueDialog} disabled={issuing}>
        <Icon name="plus" size={14} />
        Issue
      </Button>
    </div>
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
      <span>No tokens yet. Issue one to use Nymeria from another device or via the API.</span>
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
            </div>
          </div>
          {#if !isRevoked}
            <button
              class="revoke-btn"
              type="button"
              onclick={() => handleRevoke(token)}
              disabled={isRevoking}
              aria-label="Revoke token {token.token_hash_prefix}"
            >
              {#if isRevoking}
                <Icon name="loading" size={16} />
              {:else}
                <Icon name="trash" size={16} />
              {/if}
            </button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<Modal title="Issue new token" isOpen={showIssueDialog} onClose={closeIssueDialog}>
  <div class="issue-form">
    <p class="issue-intro">
      Optionally label this token so you can recognise it later.
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

  .header-actions {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
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
    padding: 10px var(--spacing-sm);
    background: rgba(var(--error-rgb), 0.08);
    border: 1px solid rgba(var(--error-rgb), 0.3);
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
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
  }

  .empty-state {
    display: flex;
    align-items: flex-start;
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
    gap: 8px;
  }

  .token-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 12px var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
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
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    background: var(--bg-base);
    padding: 2px 6px;
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
    background: rgba(var(--error-rgb), 0.12);
    border: 1px solid rgba(var(--error-rgb), 0.4);
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
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    color: var(--text-muted);
    background: transparent;
    border-radius: var(--radius-sm);
  }

  .revoke-btn:active {
    color: var(--error);
    background: rgba(var(--error-rgb), 0.1);
  }

  .revoke-btn:disabled {
    opacity: 0.5;
  }

  .issue-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(360px, 88vw);
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
    padding: 10px var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-md);
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
