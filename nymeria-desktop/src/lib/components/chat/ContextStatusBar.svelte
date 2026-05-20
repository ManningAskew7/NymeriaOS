<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import { Icon } from '$lib/components/common';

  let expanded = $state(false);

  function formatTokenCount(tokens: number): string {
    if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
    if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}k`;
    return `${tokens}`;
  }

  function getUsageColor(percentage: number): string {
    if (percentage >= 80) return 'var(--error)';
    if (percentage >= 50) return 'var(--warning)';
    return 'var(--success)';
  }
</script>

{#if chatStore.contextStats && chatStore.contextStats.totalTokens > 0}
  {@const stats = chatStore.contextStats}
  {@const color = getUsageColor(stats.usagePercentage)}
  <div class="context-status-bar" class:expanded>
    <button
      class="info-toggle"
      type="button"
      onclick={() => (expanded = !expanded)}
      title={expanded ? 'Hide context details' : `Context ${stats.usagePercentage}% used — click for details`}
      aria-label="Toggle context details"
      aria-expanded={expanded}
    >
      <span class="usage-dot" style:background={color}></span>
    </button>

    {#if expanded}
      <div class="details">
        {#if chatStore.activeModel}
          <span class="model-name">{chatStore.activeModel}</span>
          <span class="separator">|</span>
        {/if}

        <span class="token-count">{formatTokenCount(stats.totalTokens)} tokens</span>
        <span class="separator">|</span>

        <span class="usage" style:color={color}>
          {stats.usagePercentage}%
        </span>
        <div class="progress-bar">
          <div
            class="progress-fill"
            style:width="{Math.min(stats.usagePercentage, 100)}%"
            style:background={color}
          ></div>
        </div>

        {#if stats.compactionCount > 0}
          <span class="separator">|</span>
          <span class="compaction-count" title="Times compacted">{stats.compactionCount}x compacted</span>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .context-status-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
    padding: 2px 0 2px 2px;
    font-size: 0.7rem;
    color: var(--text-muted);
    background: transparent;
    user-select: none;
    flex-shrink: 0;
    min-height: 20px;
  }

  .info-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.7rem;
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .info-toggle:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .info-label {
    text-transform: lowercase;
    letter-spacing: 0.03em;
    transform: translateY(-1px);
    margin-right: -2px;
  }

  .usage-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    transform: translateY(-1px);
  }

  .details {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs, 4px);
  }

  .model-name {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    opacity: 0.8;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .separator {
    opacity: 0.3;
  }

  .token-count {
    font-family: var(--font-mono);
    font-size: 0.65rem;
  }

  .usage {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 500;
  }

  .progress-bar {
    width: 40px;
    height: 3px;
    background: var(--border-subtle);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.3s ease, background 0.3s ease;
  }

  .compaction-count {
    font-size: 0.6rem;
    opacity: 0.7;
  }
</style>
