<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';

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
  <div class="context-status-bar">
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
  </div>
{/if}

<style>
  .context-status-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
    padding: 0;
    font-size: 0.7rem;
    color: var(--text-secondary);
    background: transparent;
    user-select: none;
    flex-shrink: 0;
    min-height: 20px;
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
    opacity: 0.5;
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
