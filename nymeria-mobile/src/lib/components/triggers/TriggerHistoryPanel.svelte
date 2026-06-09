<script lang="ts">
  import type { TriggerExecution, Trigger } from '$lib/types';
  import { trapFocus } from '$lib/actions/focus';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { onMount } from 'svelte';

  interface Props {
    trigger: Trigger;
    onClose: () => void;
  }

  let { trigger, onClose }: Props = $props();

  let executions = $state<TriggerExecution[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let expandedId = $state<string | null>(null);

  onMount(async () => {
    try {
      executions = await triggersStore.getExecutions(trigger.id);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load history';
    } finally {
      loading = false;
    }
  });

  function formatTime(dateStr: string): string {
    const d = new Date(dateStr);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    const minutes = Math.floor(diff / 60000);

    if (minutes < 1) return 'Just now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function statusColor(status: string): string {
    if (status === 'success') return 'var(--success)';
    if (status === 'error') return 'var(--error)';
    if (status === 'deferred') return 'var(--text-muted)';
    return 'var(--warning)';
  }

  function toggleExpand(id: string) {
    expandedId = expandedId === id ? null : id;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="history-overlay">
  <button
    class="history-backdrop"
    type="button"
    tabindex="-1"
    aria-label="Close execution history"
    onclick={onClose}
  ></button>
  <div class="history-panel" role="dialog" aria-modal="true" aria-labelledby="trigger-history-title" tabindex="-1" use:trapFocus>
    <div class="panel-header">
      <div class="header-left">
        <Icon name="clock" size={16} />
        <h3 id="trigger-history-title">Execution History</h3>
      </div>
      <div class="header-right">
        <span class="trigger-name-badge">{trigger.name}</span>
        <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
          <Icon name="x" size={14} />
        </button>
      </div>
    </div>

    <div class="panel-body">
      {#if loading}
        <div class="center-state">
          <span class="loading-text">Loading history...</span>
        </div>
      {:else if error}
        <div class="center-state error">
          <p>{error}</p>
        </div>
      {:else if executions.length === 0}
        <div class="center-state">
          <p>No executions yet</p>
          <p class="hint">This trigger hasn't fired. Executions will appear here when it does.</p>
        </div>
      {:else}
        <div class="execution-list">
          {#each executions as exec, i (exec.id)}
            <div
              class="execution-item"
              style="animation-delay: {i * 20}ms"
            >
              <button class="exec-summary" onclick={() => toggleExpand(exec.id)} type="button" aria-expanded={expandedId === exec.id}>
                <span class="status-dot" style="background: {statusColor(exec.status)}"></span>
                <div class="exec-info">
                  <span class="exec-status" style="color: {statusColor(exec.status)}">{exec.status}</span>
                  <span class="exec-time">{formatTime(exec.timestamp)}</span>
                </div>
                <div class="exec-meta">
                  {#if exec.event_count > 1}
                    <span class="event-count">{exec.event_count} events</span>
                  {/if}
                  <span class="duration">{exec.duration_seconds.toFixed(1)}s</span>
                  <span class="action-type">{exec.action_type.replace('_', ' ')}</span>
                </div>
                <span class="expand-chevron" class:rotated={expandedId === exec.id}>
                  <Icon name="chevronRight" size={12} />
                </span>
              </button>

              {#if expandedId === exec.id}
                <div class="exec-details">
                  {#if exec.events_summary}
                    <div class="detail-section">
                      <span class="detail-label">Events</span>
                      <pre class="detail-content">{exec.events_summary}</pre>
                    </div>
                  {/if}
                  {#if exec.response_summary}
                    <div class="detail-section">
                      <span class="detail-label">Response</span>
                      <pre class="detail-content">{exec.response_summary}</pre>
                    </div>
                  {/if}
                  {#if exec.error_message}
                    <div class="detail-section error-section">
                      <span class="detail-label">Error</span>
                      <pre class="detail-content error-text">{exec.error_message}</pre>
                    </div>
                  {/if}
                  <div class="detail-footer">
                    <span>ID: {exec.id}</span>
                    <span>{new Date(exec.timestamp).toLocaleString()}</span>
                  </div>
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .history-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.15s ease-out;
  }

  .history-backdrop {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(2px);
  }

  .history-panel {
    position: relative;
    width: 500px;
    max-width: 90vw;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.3);
    animation: slideUp 0.2s ease-out;
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes slideUp {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    color: var(--text-primary);
  }

  .header-left h3 {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: 600;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .trigger-name-badge {
    font-size: var(--font-size-2xs);
    padding: 1px 8px;
    background: rgba(var(--accent-primary-rgb), 0.1);
    color: var(--accent-primary);
    border-radius: var(--radius-sm);
    font-weight: 500;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-sm);
  }

  .center-state {
    text-align: center;
    padding: var(--spacing-xl) var(--spacing-md);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  .center-state.error {
    color: var(--error);
  }

  .center-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .center-state p {
    margin: 0;
  }

  .execution-list {
    display: flex;
    flex-direction: column;
  }

  .execution-item {
    border-bottom: 1px solid var(--glass-border);
    animation: itemFadeIn 0.2s ease-out both;
  }

  .execution-item:last-child {
    border-bottom: none;
  }

  @keyframes itemFadeIn {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .exec-summary {
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-xs);
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .exec-summary:hover {
    background: var(--bg-elevated-2);
  }

  .status-dot {
    flex-shrink: 0;
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .exec-info {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 70px;
  }

  .exec-status {
    font-size: var(--font-size-2xs);
    font-weight: 600;
    text-transform: capitalize;
  }

  .exec-time {
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  .exec-meta {
    flex: 1;
    display: flex;
    gap: var(--spacing-xs);
    justify-content: flex-end;
    align-items: center;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  .event-count,
  .duration,
  .action-type {
    padding: 1px 5px;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-sm);
  }

  .exec-details {
    padding: var(--spacing-xs) var(--spacing-sm) var(--spacing-sm);
    margin-left: 20px;
    border-left: 2px solid var(--glass-border);
    animation: fadeIn 0.15s ease-out;
  }

  .detail-section {
    margin-bottom: var(--spacing-sm);
  }

  .detail-label {
    display: block;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    color: var(--text-muted);
    letter-spacing: 0.05em;
    margin-bottom: 2px;
  }

  .detail-content {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 120px;
    overflow-y: auto;
    font-family: var(--font-mono);
    line-height: 1.4;
  }

  .error-section .detail-content {
    border-left: 2px solid var(--error);
  }

  .error-text {
    color: var(--error);
  }

  .detail-footer {
    display: flex;
    justify-content: space-between;
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
    padding-top: var(--spacing-xs);
    border-top: 1px solid var(--glass-border);
  }

  .expand-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }

  .expand-chevron.rotated {
    transform: rotate(90deg);
  }
</style>
