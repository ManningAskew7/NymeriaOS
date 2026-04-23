<script lang="ts">
  import type { ToolReloadInfo } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    info: ToolReloadInfo;
  }

  let { info }: Props = $props();

  let expanded = $state(false);

  function ttlLabel(ttl: string): string {
    if (ttl === 'permanent') return 'permanent';
    return ttl || '2h';
  }
</script>

<div class="reload-indicator">
  <div class="connector-line"></div>
  <button class="pill" onclick={() => expanded = !expanded} type="button">
    <span class="icon">
      <Icon name="cog" size={13} />
    </span>
    <span class="label">Tool Binding</span>
    <span class="tools">{info.tools.join(', ')}</span>
    <span class="ttl">{ttlLabel(info.ttl)}</span>
    <span class="chevron" class:open={expanded}>
      <Icon name="chevronRight" size={12} />
    </span>
  </button>
  {#if expanded && info.resumePrompt}
    <div class="prompt-detail">
      <code>{info.resumePrompt}</code>
    </div>
  {/if}
  <div class="connector-line"></div>
</div>

<style>
  .reload-indicator {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: var(--spacing-xs) 0;
  }

  .connector-line {
    width: 1px;
    height: 12px;
    background: var(--border-subtle);
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 999px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    font-size: 0.75rem;
    line-height: 1;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .pill:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
  }

  .icon {
    display: flex;
    align-items: center;
    color: var(--text-tertiary);
  }

  .label {
    font-weight: 600;
    color: var(--text-secondary);
  }

  .tools {
    color: var(--text-tertiary);
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ttl {
    color: var(--text-tertiary);
    opacity: 0.7;
  }

  .chevron {
    display: flex;
    align-items: center;
    color: var(--text-tertiary);
    transition: transform var(--transition-fast);
  }

  .chevron.open {
    transform: rotate(90deg);
  }

  .prompt-detail {
    margin-top: 4px;
    padding: 6px 12px;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    max-width: 500px;
    animation: fadeSlide 150ms ease-out;
  }

  .prompt-detail code {
    font-size: 0.7rem;
    color: var(--text-tertiary);
    word-break: break-word;
    white-space: pre-wrap;
  }

  @keyframes fadeSlide {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
