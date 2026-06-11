<script lang="ts">
  import { fly } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import type { ErrorKind } from '$lib/stores/errors.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { TOAST_DROP_IN, TOAST_DROP_OUT, TOAST_FLIP } from '$lib/utils/transitions';
  import Icon from './Icon.svelte';

  function iconFor(kind: ErrorKind): string {
    switch (kind) {
      case 'auth_invalid':
      case 'account_disabled':
      case 'last_admin':
      case 'resource_owned':
        return 'warning';
      case 'forbidden_admin':
      case 'forbidden_owner':
        return 'info';
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
      case 'auth_invalid': return 'Session expired';
      case 'account_disabled': return 'Account disabled';
      case 'forbidden_admin': return 'Admin role required';
      case 'forbidden_owner': return 'Not your resource';
      case 'last_admin': return 'Last admin';
      case 'resource_owned': return 'Cannot delete';
      default: return "That didn't work";
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
      in:fly={TOAST_DROP_IN}
      out:fly={TOAST_DROP_OUT}
      animate:flip={TOAST_FLIP}
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
        <Icon name="x" size={16} />
      </button>
    </div>
  {/each}
</div>

<style>
  .toast-stack {
    position: fixed;
    top: calc(var(--safe-area-top) + var(--spacing-sm));
    left: var(--spacing-sm);
    right: var(--spacing-sm);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    z-index: 2000;
    pointer-events: none;
  }

  .toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: 10px var(--spacing-sm);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border: 1px solid var(--border-default);
    border-left-width: 3px;
    border-radius: var(--radius-md);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    color: var(--text-primary);
  }

  .toast.destructive {
    border-left-color: var(--error);
  }
  .toast.destructive :global(svg) {
    color: var(--error);
  }
  .toast.warning {
    border-left-color: var(--warning);
  }
  .toast.warning :global(svg) {
    color: var(--warning);
  }
  .toast.info {
    border-left-color: var(--accent-primary);
  }
  .toast.info :global(svg) {
    color: var(--accent-primary);
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
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
  }

  .toast-close {
    color: var(--text-muted);
    background: transparent;
    padding: 6px;
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }
</style>
