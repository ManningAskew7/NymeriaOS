<script lang="ts">
  import type { ToolCall, WorkspaceArtifact } from '$lib/types';
  import { Collapsible, Icon } from '$lib/components/common';
  import { formatFileSize } from '$lib/utils/fileProcessing';
  import WorkspaceArtifactModal from './WorkspaceArtifactModal.svelte';

  interface Props {
    toolCall: ToolCall;
  }

  let { toolCall }: Props = $props();
  let modalArtifact = $state<WorkspaceArtifact | null>(null);

  let duration = $derived.by(() => {
    if (!toolCall.startTime || !toolCall.endTime) return null;
    const ms = toolCall.endTime.getTime() - toolCall.startTime.getTime();
    if (ms < 1000) return `${ms}ms`;
    const s = ms / 1000;
    return s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`;
  });

  function formatArgs(args: Record<string, unknown>): string {
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      return String(args);
    }
  }

  function formatResult(result: string | undefined): string {
    if (!result) return '';
    try {
      const parsed = JSON.parse(result);
      return JSON.stringify(parsed, null, 2);
    } catch {
      return result;
    }
  }

  function getArtifactIcon(mimeType: string): string {
    return mimeType.startsWith('image/') ? 'image' : 'fileText';
  }
</script>

<div class="tool-call-card" class:running={toolCall.status === 'running'} class:cancelled={toolCall.status === 'cancelled'}>
  <Collapsible title={toolCall.name} chevronIcon="terminal" chevronSize={20}>
    {#snippet header()}
      <div class="tool-header">
        <span class="tool-name">{toolCall.name}</span>
        {#if duration}
          <span class="duration-badge">{duration}</span>
        {:else if toolCall.status === 'cancelled'}
          <span class="duration-badge cancelled-badge">Cancelled</span>
        {/if}
      </div>
    {/snippet}

    <div class="tool-details">
      <div class="detail-section">
        <h4>Arguments</h4>
        <pre class="code-block">{formatArgs(toolCall.arguments)}</pre>
      </div>

      {#if toolCall.result}
        <div class="detail-section">
          <h4>Result</h4>
          <pre class="code-block result" class:error={toolCall.status === 'error'}>
            {formatResult(toolCall.result)}
          </pre>
        </div>
      {/if}

      {#if toolCall.artifacts?.length}
        <div class="detail-section">
          <h4>Artifacts</h4>
          <div class="artifact-list">
            {#each toolCall.artifacts as artifact (artifact.path)}
              <button
                type="button"
                class="artifact-chip"
                onclick={() => { modalArtifact = artifact; }}
                title={`View ${artifact.name}`}
              >
                <Icon name={getArtifactIcon(artifact.mimeType)} size={20} />
                <span class="artifact-copy">
                  <span class="artifact-name">{artifact.name}</span>
                  <span class="artifact-meta">{formatFileSize(artifact.sizeBytes)} • {artifact.mimeType}</span>
                </span>
                <Icon name="chevronRight" size={18} />
              </button>
            {/each}
          </div>
        </div>
      {/if}

      {#if toolCall.startTime}
        <div class="timing">
          <span>Started: {toolCall.startTime.toLocaleTimeString()}</span>
          {#if toolCall.endTime}
            <span>
              Duration: {Math.round((toolCall.endTime.getTime() - toolCall.startTime.getTime()) / 1000)}s
            </span>
          {/if}
        </div>
      {/if}
    </div>
  </Collapsible>
</div>

<WorkspaceArtifactModal artifact={modalArtifact} onClose={() => { modalArtifact = null; }} />

<style>
  .tool-call-card {
    position: relative;
    background: var(--glass-bg);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-radius: var(--radius-md);
    border: 1px solid var(--glass-border);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }

  .tool-call-card.running {
    border-color: color-mix(in srgb, var(--accent-primary) 30%, var(--glass-border));
  }

  .tool-call-card.cancelled {
    border-color: color-mix(in srgb, var(--text-muted) 30%, var(--glass-border));
    opacity: 0.7;
  }

  .cancelled-badge {
    color: var(--text-muted);
    font-style: italic;
  }

  /* Gradient wave animation — accent-colored band sweeps left to right */
  .tool-call-card.running::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(
      90deg,
      transparent 0%,
      transparent 30%,
      var(--accent-primary) 50%,
      transparent 70%,
      transparent 100%
    );
    opacity: 0.1;
    transform: translateX(-100%);
    animation: toolWave 2s ease-in-out infinite;
    pointer-events: none;
  }

  @keyframes toolWave {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
  }

  /* Reduce header height ~15% by tightening vertical padding */
  .tool-call-card :global(.header) {
    padding-top: 6px;
    padding-bottom: 6px;
  }

  .tool-call-card :global(.chevron) {
    color: var(--accent-primary);
  }

  .tool-name {
    font-weight: 500;
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
  }

  .duration-badge {
    margin-left: auto;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-family: var(--font-mono);
  }

  .tool-details {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .detail-section h4 {
    margin: 0 0 var(--spacing-xs) 0;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .code-block {
    margin: 0;
    padding: var(--spacing-sm);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 200px;
    overflow-y: auto;
  }

  .code-block.result.error {
    border-left: 3px solid var(--error);
  }

  .timing {
    display: flex;
    gap: var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .artifact-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .artifact-chip {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: 48px;
    text-align: left;
    padding: 0.75rem 0.8rem;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    cursor: pointer;
    touch-action: manipulation;
  }

  .artifact-chip:active {
    border-color: color-mix(in srgb, var(--accent-primary) 50%, var(--border-default));
    transform: scale(0.99);
  }

  .artifact-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .artifact-name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-meta {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
