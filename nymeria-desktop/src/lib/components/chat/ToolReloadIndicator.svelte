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

  const toolNames = $derived(
    info.tools.length > 0 ? info.tools.join(', ') : ''
  );

  const promptText = $derived(
    info.resumePrompt ||
    (info.tools.length > 0
      ? `[System: tools ${info.tools.join(', ')} are now loaded ${info.ttl === 'permanent' ? 'permanently' : `for the next ${info.ttl || '2h'}`}. Continue the user's task using the new tools.]`
      : `[System: tools reloaded ${info.ttl === 'permanent' ? 'permanently' : `for the next ${info.ttl || '2h'}`}. Continue the user's task using the new tools.]`)
  );
</script>

<div class="reload-indicator">
  <div class="connector-line"></div>
  <div class="pill-row">
    <button class="pill" onclick={() => expanded = !expanded} type="button">
      <span class="icon">
        <Icon name="cog" size={12} />
      </span>
      <span class="label">Tool Binding</span>
      {#if toolNames}
        <span class="sep">-</span>
        <span class="tools">{toolNames}</span>
      {/if}
      <span class="ttl">({ttlLabel(info.ttl)})</span>
      <span class="chevron" class:open={expanded}>
        <Icon name="chevronRight" size={10} />
      </span>
    </button>
  </div>
  {#if expanded}
    <div class="prompt-detail">
      <code>{promptText}</code>
    </div>
  {/if}
  <div class="connector-line"></div>
</div>

<style>
  .reload-indicator {
    align-self: flex-start;
    display: flex;
    flex-direction: column;
    align-items: center;
    max-width: 85%;
    margin-bottom: 0;
  }

  .connector-line {
    width: 1px;
    height: 14px;
    background: var(--border-subtle);
  }

  .pill-row {
    display: flex;
    justify-content: center;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 999px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    color: var(--text-tertiary);
    font-size: 0.7rem;
    line-height: 1;
    white-space: nowrap;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .pill:hover {
    background: var(--bg-hover);
    border-color: var(--border-default);
    color: var(--text-secondary);
  }

  .icon {
    display: flex;
    align-items: center;
  }

  .label {
    font-weight: 600;
  }

  .sep {
    opacity: 0.4;
  }

  .tools {
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .ttl {
    opacity: 0.6;
  }

  .chevron {
    display: flex;
    align-items: center;
    transition: transform var(--transition-fast);
  }

  .chevron.open {
    transform: rotate(90deg);
  }

  .prompt-detail {
    margin-top: 2px;
    padding: 6px 12px;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    animation: fadeSlide 150ms ease-out;
  }

  .prompt-detail code {
    font-size: 0.68rem;
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
