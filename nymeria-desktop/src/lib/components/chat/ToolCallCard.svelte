<script lang="ts">
  import type { ToolCall } from '$lib/types';
  import { Collapsible, Icon, Spinner } from '$lib/components/common';

  interface Props {
    toolCall: ToolCall;
  }

  let { toolCall }: Props = $props();

  let statusIcon = $derived.by(() => {
    switch (toolCall.status) {
      case 'running':
        return null; // Will show spinner
      case 'success':
        return 'success';
      case 'error':
        return 'error';
      default:
        return 'clock';
    }
  });

  let statusColor = $derived.by(() => {
    switch (toolCall.status) {
      case 'running':
        return 'var(--accent-primary)';
      case 'success':
        return 'var(--success)';
      case 'error':
        return 'var(--error)';
      default:
        return 'var(--text-muted)';
    }
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
      // Try to parse and pretty-print JSON
      const parsed = JSON.parse(result);
      return JSON.stringify(parsed, null, 2);
    } catch {
      return result;
    }
  }
</script>

<div class="tool-call-card" class:running={toolCall.status === 'running'}>
  <Collapsible title={toolCall.name}>
    {#snippet header()}
      <div class="tool-header">
        <div class="status-indicator" style="color: {statusColor}">
          {#if toolCall.status === 'running'}
            <Spinner size="sm" />
          {:else if statusIcon}
            <Icon name={statusIcon} size={16} />
          {/if}
        </div>
        <Icon name="tool" size={16} class="tool-icon" />
        <span class="tool-name">{toolCall.name}</span>
        {#if toolCall.status === 'running'}
          <span class="status-text">Running...</span>
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

<style>
  .tool-call-card {
    background: var(--glass-bg);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-radius: var(--radius-md);
    border: 1px solid var(--glass-border);
    overflow: hidden;
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  }

  .tool-call-card.running {
    border-color: var(--accent-primary);
    animation: glowPulse 2s ease-in-out infinite;
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
  }

  .status-indicator {
    display: flex;
    align-items: center;
  }

  :global(.tool-icon) {
    color: var(--accent-primary);
  }

  .tool-name {
    font-weight: 500;
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
  }

  .status-text {
    margin-left: auto;
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    font-style: italic;
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
</style>
