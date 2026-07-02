<script lang="ts">
  import type { WorkflowApproval } from '$lib/types';
  import { workflowsStore } from '$lib/stores/workflows.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';

  interface Props {
    approval: WorkflowApproval;
  }

  let { approval }: Props = $props();

  let resolving = $state<'approve' | 'decline' | null>(null);
  let resolveError = $state<string | null>(null);

  // "expires in 6d" ladder, mirroring formatRelativeTime's granularity but
  // pointed forward. Only used here, so it stays component-local.
  function formatExpiry(iso: string): string {
    const ms = new Date(iso).getTime() - Date.now();
    if (!Number.isFinite(ms)) return '';
    if (ms <= 0) return 'expiring now';
    const minutes = Math.floor(ms / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `expires in ${days}d`;
    if (hours > 0) return `expires in ${hours}h`;
    if (minutes > 0) return `expires in ${minutes}m`;
    return 'expiring now';
  }

  let expiry = $derived(formatExpiry(approval.expires_at));

  async function resolve(approved: boolean) {
    if (resolving) return;
    resolving = approved ? 'approve' : 'decline';
    resolveError = null;
    try {
      await workflowsStore.resolveApproval(approval.record_id, approved);
    } catch (e) {
      resolveError = humanizeErrorText(e, {
        action: approved ? 'approve' : 'decline',
        resource: 'this workflow request',
      });
    } finally {
      resolving = null;
    }
  }
</script>

<div class="approval-item">
  <div class="approval-body">
    <p class="approval-prompt">{approval.prompt || 'Workflow paused for your approval'}</p>
    <div class="approval-meta">
      <span class="workflow-name" use:tooltipWhenClipped={approval.workflow_id}>{approval.workflow_id}</span>
      {#if expiry}
        <span class="approval-expiry">{expiry}</span>
      {/if}
    </div>
    {#if resolveError}
      <p class="approval-error" role="alert">{resolveError}</p>
    {/if}
  </div>
  <div class="approval-actions">
    <button
      class="action-btn approve"
      type="button"
      onclick={() => resolve(true)}
      disabled={resolving !== null}
    >
      {resolving === 'approve' ? 'Approving…' : 'Approve'}
    </button>
    <button
      class="action-btn decline"
      type="button"
      onclick={() => resolve(false)}
      disabled={resolving !== null}
    >
      {resolving === 'decline' ? 'Declining…' : 'Decline'}
    </button>
  </div>
</div>

<style>
  .approval-item {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    animation: staggerFadeIn var(--transition-slow) backwards;
  }

  .approval-item:nth-child(1) { animation-delay: 0.03s; }
  .approval-item:nth-child(2) { animation-delay: 0.06s; }
  .approval-item:nth-child(3) { animation-delay: 0.09s; }
  .approval-item:nth-child(4) { animation-delay: 0.12s; }
  .approval-item:nth-child(5) { animation-delay: 0.15s; }
  .approval-item:nth-child(6) { animation-delay: 0.18s; }
  .approval-item:nth-child(7) { animation-delay: 0.21s; }
  .approval-item:nth-child(8) { animation-delay: 0.24s; }
  .approval-item:nth-child(9) { animation-delay: 0.27s; }
  .approval-item:nth-child(10) { animation-delay: 0.3s; }

  .approval-item:hover {
    background: var(--bg-hover);
  }

  .approval-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .approval-prompt {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    line-height: 1.4;
    word-break: break-word;
  }

  .approval-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    min-height: 18px;
  }

  .workflow-name {
    max-width: 140px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .approval-expiry {
    margin-left: auto;
    flex-shrink: 0;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
    white-space: nowrap;
  }

  .approval-error {
    margin: 0;
    font-size: var(--font-size-2xs);
    color: var(--error);
    line-height: 1.4;
  }

  .approval-actions {
    display: flex;
    gap: var(--spacing-xs);
  }

  .action-btn {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    background: transparent;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .action-btn.approve:not(:disabled):hover {
    border-color: color-mix(in srgb, var(--success) 60%, transparent);
    background: color-mix(in srgb, var(--success) 12%, transparent);
    color: var(--success);
  }

  .action-btn.decline:not(:disabled):hover {
    border-color: color-mix(in srgb, var(--error) 50%, transparent);
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
  }

  .action-btn:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }
</style>
