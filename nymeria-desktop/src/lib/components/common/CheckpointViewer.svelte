<script lang="ts">
  import { Icon, Modal } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';

  interface Props {
    threadId: string | null;
    onClose: () => void;
  }

  let { threadId, onClose }: Props = $props();

  let loading = $state(false);
  let status = $state<'idle' | 'error'>('idle');
  let message = $state('');
  let text = $state('');
  let summary = $state<{ checkpointId: unknown; messageCount: unknown } | null>(null);
  let copied = $state(false);

  // Load on open and whenever the target thread changes. Mirrors
  // WorkspaceArtifactModal: a cancelled guard prevents a stale fetch (from a
  // previous threadId) clobbering the current view.
  $effect(() => {
    const currentThreadId = threadId;

    status = 'idle';
    message = '';
    text = '';
    summary = null;
    copied = false;

    if (!currentThreadId) {
      loading = false;
      return;
    }

    let cancelled = false;
    loading = true;

    void (async () => {
      try {
        const data = await api.getThreadCheckpoint(currentThreadId);
        if (cancelled) return;
        text = JSON.stringify(data, null, 2);
        const record = (data ?? {}) as Record<string, unknown>;
        summary = {
          checkpointId: record.checkpoint_id ?? null,
          messageCount: record.message_count ?? null,
        };
      } catch (err) {
        if (!cancelled) {
          status = 'error';
          message = err instanceof Error ? err.message : 'Failed to load checkpoint';
        }
      } finally {
        if (!cancelled) loading = false;
      }
    })();

    return () => {
      cancelled = true;
    };
  });

  async function copyToClipboard() {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      // Clipboard may be unavailable (e.g. insecure context); fail quietly.
    }
  }

  function downloadJson() {
    if (!threadId || !text) return;
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `checkpoint-${threadId}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }
</script>

<Modal title="Raw checkpoint" isOpen={threadId !== null} {onClose}>
  {#snippet children()}
    <div class="checkpoint-viewer">
      <div class="toolbar">
        <div class="meta">
          <code class="thread-id">{threadId}</code>
          {#if summary}
            <span class="detail">checkpoint {summary.checkpointId ?? '(none)'}</span>
            <span class="detail">{summary.messageCount ?? 0} messages</span>
          {/if}
        </div>
        <div class="actions">
          <button
            type="button"
            class="action-btn secondary"
            onclick={copyToClipboard}
            disabled={!text}
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button type="button" class="action-btn" onclick={downloadJson} disabled={!text}>
            Download
          </button>
        </div>
      </div>

      {#if loading}
        <div class="state">
          <Icon name="loading" size={22} />
          <span>Loading checkpoint...</span>
        </div>
      {:else if status === 'error'}
        <div class="state error">
          <Icon name="error" size={22} />
          <span>{message}</span>
        </div>
      {:else}
        <pre class="json-viewer">{text}</pre>
      {/if}
    </div>
  {/snippet}
</Modal>

<style>
  .checkpoint-viewer {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(900px, 82vw);
    min-height: min(560px, 74vh);
  }

  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-md);
  }

  .meta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    min-width: 0;
  }

  .thread-id {
    display: block;
    max-width: 100%;
    overflow-x: auto;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
  }

  .detail {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    flex-shrink: 0;
  }

  .action-btn {
    border: 1px solid var(--accent-primary);
    background: var(--accent-primary);
    color: white;
    border-radius: var(--radius-sm);
    padding: 0.5rem 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity var(--transition-fast), transform var(--transition-fast);
  }

  .action-btn.secondary {
    background: transparent;
    color: var(--text-primary);
    border-color: var(--border-default);
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn:not(:disabled):hover {
    opacity: 0.9;
    transform: translateY(-1px);
  }

  .state {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    min-height: 420px;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--bg-elevated) 75%, transparent);
    text-align: center;
    padding: var(--spacing-lg);
  }

  .state.error {
    color: var(--error);
  }

  .json-viewer {
    margin: 0;
    min-height: 420px;
    max-height: 68vh;
    overflow: auto;
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
    white-space: pre;
    color: var(--text-primary);
  }

  @media (max-width: 900px) {
    .checkpoint-viewer {
      min-width: auto;
      min-height: auto;
    }

    .toolbar {
      flex-direction: column;
    }

    .actions {
      width: 100%;
      justify-content: flex-end;
    }
  }
</style>
