<script lang="ts">
  import type { ToolCall } from '$lib/types';
  import { Collapsible } from '$lib/components/common';

  interface Props {
    toolCall: ToolCall;
  }

  let { toolCall }: Props = $props();

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
</script>

<div class="tool-call-card" class:running={toolCall.status === 'running'}>
  <Collapsible title={toolCall.name} chevronIcon="terminal" chevronSize={20}>
    {#snippet header()}
      <div class="tool-header">
        <span class="tool-name">{toolCall.name}</span>
        {#if duration}
          <span class="duration-badge">{duration}</span>
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
</style>
