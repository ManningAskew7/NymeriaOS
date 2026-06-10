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
  }

  let { isOpen, onClose, rawToken, label, forUser }: Props = $props();

  let copied = $state(false);
  let copyError = $state<string | null>(null);

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
      <Button variant="ghost" onclick={onClose}>I've saved it</Button>
    </div>

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
    min-width: min(420px, 90vw);
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
    font-size: 13px;
    color: var(--text-primary);
    user-select: all;
    word-break: break-all;
    white-space: pre-wrap;
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    align-items: center;
  }

  .copy-error {
    margin: 0;
    font-size: 12px;
    color: var(--error);
  }
</style>
