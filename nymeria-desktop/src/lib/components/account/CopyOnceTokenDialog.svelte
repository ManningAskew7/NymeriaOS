<script lang="ts">
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Modal from '$lib/components/common/Modal.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  interface Props {
    isOpen: boolean;
    onClose: () => void;
    /** Raw token returned by /me/tokens or /admin/users/{id}/tokens. Shown once. */
    rawToken: string | null;
    /** Optional human label for the new token, surfaced in the header. */
    label?: string | null;
    /** Optional context line, e.g. "for alice@example.com" when an admin issues for someone else. */
    forUser?: string | null;
    /** Optional action shown while the raw token is still available. */
    onSaveAccount?: (() => void | Promise<void>) | null;
    /** Optional action to save the token as a switchable account and activate it. */
    onSaveAndSwitch?: (() => void | Promise<void>) | null;
    savingAccount?: boolean;
    accountActionMessage?: string | null;
    accountActionError?: string | null;
  }

  let {
    isOpen,
    onClose,
    rawToken,
    label,
    forUser,
    onSaveAccount = null,
    onSaveAndSwitch = null,
    savingAccount = false,
    accountActionMessage = null,
    accountActionError = null,
  }: Props = $props();

  let copied = $state(false);
  let copyError = $state<string | null>(null);

  // Reset feedback whenever the dialog re-opens with a fresh token.
  $effect(() => {
    if (isOpen) {
      copied = false;
      copyError = null;
    }
  });

  async function handleCopy() {
    if (!rawToken) return;
    copyError = null;
    try {
      await navigator.clipboard.writeText(rawToken);
      copied = true;
    } catch (e) {
      copyError = humanizeErrorText(e, { action: 'copy', resource: 'the token' });
    }
  }
</script>

<Modal title="New token issued" {isOpen} {onClose}>
  <div class="copy-once">
    <div class="warning-banner">
      <Icon name="warning" size={16} />
      <span>This token won't be shown again. Copy it now.</span>
    </div>

    <p class="intro">
      {#if forUser}
        Issued for <strong>{forUser}</strong>{label ? ` (${label})` : ''}. Hand it
        off securely; anyone with this string can act as that account.
      {:else}
        Use this token in another browser, the mobile app, or any tool calling
        the API. It carries the same access as your current session.
      {/if}
    </p>

    <div class="token-block">
      <code class="token-text">{rawToken ?? ''}</code>
    </div>

    <div class="actions">
      <Button variant="primary" onclick={handleCopy} disabled={!rawToken}>
        <Icon name={copied ? 'check' : 'copy'} size={14} />
        {copied ? 'Copied!' : 'Copy token'}
      </Button>
      {#if onSaveAccount}
        <Button
          variant="secondary"
          onclick={() => void onSaveAccount?.()}
          disabled={!rawToken || savingAccount}
        >
          <Icon name={savingAccount ? 'loading' : 'server'} size={14} />
          {savingAccount ? 'Saving…' : 'Save account'}
        </Button>
      {/if}
      {#if onSaveAndSwitch}
        <Button
          variant="secondary"
          onclick={() => void onSaveAndSwitch?.()}
          disabled={!rawToken || savingAccount}
        >
          <Icon name={savingAccount ? 'loading' : 'check'} size={14} />
          {savingAccount ? 'Switching…' : 'Save and switch'}
        </Button>
      {/if}
      <Button variant="ghost" onclick={onClose}>I've saved it</Button>
    </div>

    {#if accountActionMessage}
      <p class="account-action-message">{accountActionMessage}</p>
    {/if}
    {#if accountActionError}
      <p class="account-action-error">{accountActionError}</p>
    {/if}

    {#if copyError}
      <p class="copy-error">
        Copy failed: {copyError}. You can still select and copy the token manually.
      </p>
    {/if}
  </div>
</Modal>

<style>
  .copy-once {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(440px, 90vw);
  }

  .warning-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px var(--spacing-sm);
    background: rgba(var(--warning-rgb), 0.12);
    border: 1px solid rgba(var(--warning-rgb), 0.4);
    border-radius: var(--radius-sm);
    color: var(--warning);
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .intro {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .intro strong {
    color: var(--text-primary);
  }

  .token-block {
    background: var(--bg-base);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
    overflow-x: auto;
  }

  .token-text {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    user-select: all;
    word-break: break-all;
    white-space: pre-wrap;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-sm);
    align-items: center;
  }

  .account-action-message,
  .account-action-error,
  .copy-error {
    margin: 0;
    font-size: var(--font-size-xs);
  }

  .account-action-message {
    color: var(--success);
  }

  .account-action-error,
  .copy-error {
    color: var(--error);
  }
</style>
