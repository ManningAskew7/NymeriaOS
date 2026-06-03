<script lang="ts">
  import type { ErrorKind } from '$lib/stores/errors.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import Icon from './Icon.svelte';

  function iconFor(kind: ErrorKind): string {
    switch (kind) {
      case 'auth_invalid':
      case 'account_disabled':
        return 'warning';
      case 'forbidden_admin':
      case 'forbidden_owner':
        return 'info';
      case 'last_admin':
      case 'resource_owned':
        return 'warning';
      default:
        return 'error';
    }
  }

  function severity(kind: ErrorKind): 'destructive' | 'warning' | 'info' {
    switch (kind) {
      case 'auth_invalid':
      case 'account_disabled':
        return 'destructive';
      case 'last_admin':
      case 'resource_owned':
        return 'warning';
      case 'forbidden_admin':
      case 'forbidden_owner':
        return 'info';
      default:
        return 'destructive';
    }
  }

  function titleFor(kind: ErrorKind): string {
    switch (kind) {
      case 'auth_invalid':
        return 'Session expired';
      case 'account_disabled':
        return 'Account disabled';
      case 'forbidden_admin':
        return 'Admin role required';
      case 'forbidden_owner':
        return 'Not your resource';
      case 'last_admin':
        return 'Last admin';
      case 'resource_owned':
        return 'Cannot delete';
      default:
        return 'Something went wrong';
    }
  }
</script>

<div class="toast-stack" aria-live="polite" aria-atomic="false">
  {#each errorsStore.queue as toast (toast.id)}
    <div
      class="toast"
      class:destructive={severity(toast.kind) === 'destructive'}
      class:warning={severity(toast.kind) === 'warning'}
      class:info={severity(toast.kind) === 'info'}
      role="status"
    >
      <Icon name={iconFor(toast.kind)} size={18} />
      <div class="toast-body">
        <div class="toast-title">{titleFor(toast.kind)}</div>
        <div class="toast-message">{toast.message}</div>
        {#if toast.action}
          <button
            class="toast-action"
            type="button"
            onclick={() => {
              toast.action?.onClick();
              errorsStore.dismiss(toast.id);
            }}
          >
            {toast.action.label}
          </button>
        {/if}
      </div>
      <button
        class="toast-close"
        type="button"
        aria-label="Dismiss"
        onclick={() => errorsStore.dismiss(toast.id)}
      >
        <Icon name="x" size={14} />
      </button>
    </div>
  {/each}
</div>

<style>
  .toast-stack {
    position: fixed;
    top: var(--spacing-md);
    right: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    z-index: 2000;
    pointer-events: none;
    max-width: min(420px, calc(100vw - 2 * var(--spacing-md)));
  }

  .toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2, var(--bg-elevated));
    /* §7 — floating toast: shadow alone defines elevation; an all-around
       border would be redundant chrome. The 3px left-edge stays because
       it is SEMANTIC — the per-severity rules below override its color
       to signal destructive / warning / info. */
    border-left: 3px solid var(--border-default);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
    color: var(--text-primary);
    animation: toast-in 180ms ease-out both;
  }

  .toast.destructive {
    border-left-color: var(--error, #ef4444);
    color: var(--text-primary);
  }

  .toast.destructive :global(svg) {
    color: var(--error, #ef4444);
  }

  .toast.warning {
    border-left-color: var(--warning, #fbbf24);
  }

  .toast.warning :global(svg) {
    color: var(--warning, #fbbf24);
  }

  .toast.info {
    border-left-color: var(--accent-primary);
  }

  .toast.info :global(svg) {
    color: var(--accent-primary);
  }

  @keyframes toast-in {
    from {
      opacity: 0;
      transform: translateX(8px);
    }
    to {
      opacity: 1;
      transform: translateX(0);
    }
  }

  .toast-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .toast-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .toast-message {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .toast-action {
    margin-top: 6px;
    align-self: flex-start;
    color: var(--accent-primary);
    background: transparent;
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    font-weight: 600;
  }

  .toast-action:hover {
    background: var(--accent-primary-alpha, rgba(34, 211, 238, 0.12));
  }

  .toast-close {
    color: var(--text-muted);
    background: transparent;
    padding: 4px;
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }

  .toast-close:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
</style>
