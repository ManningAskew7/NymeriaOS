<script lang="ts">
  import Icon from '$lib/components/common/Icon.svelte';
  import { renderMarkdown, renderMarkdownStreaming } from '$lib/utils/markdown';

  interface Props {
    content: string;
    isActivelyStreaming?: boolean;
  }

  let { content, isActivelyStreaming = false }: Props = $props();

  // Start open only if actively streaming when first mounted
  let isOpen = $state(isActivelyStreaming);

  // Track previous streaming state for auto-collapse transition
  let wasStreaming = $state(isActivelyStreaming);

  $effect(() => {
    if (isActivelyStreaming && !wasStreaming) {
      // Started streaming -> open
      isOpen = true;
    } else if (!isActivelyStreaming && wasStreaming) {
      // Stopped streaming -> auto-collapse
      isOpen = false;
    }
    wasStreaming = isActivelyStreaming;
  });

  function toggle() {
    isOpen = !isOpen;
  }
</script>

<div class="thinking-block" class:open={isOpen}>
  <button class="thinking-header" onclick={toggle} type="button">
    <span class="thinking-chevron">
      <Icon name="chevronRight" size={14} />
    </span>
    {#if isActivelyStreaming}
      <span class="thinking-dots">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </span>
      <span class="thinking-label streaming">Thinking…</span>
    {:else}
      <span class="thinking-label">Thought</span>
    {/if}
  </button>

  {#if isOpen}
    <div class="thinking-content">
      <div class="markdown-content">
        {@html isActivelyStreaming ? renderMarkdownStreaming(content) : renderMarkdown(content)}
      </div>
    </div>
  {/if}
</div>

<style>
  .thinking-block {
    border-radius: var(--radius-md);
    overflow: hidden;
    margin-bottom: var(--spacing-sm);
  }

  .thinking-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    color: var(--text-secondary);
    text-align: left;
    font-size: var(--font-size-sm);
    border: none;
    cursor: pointer;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .thinking-header:hover {
    background: color-mix(in srgb, var(--text-muted) 10%, transparent);
  }

  .thinking-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    transition: transform var(--transition-normal) cubic-bezier(0.4, 0, 0.2, 1);
    flex-shrink: 0;
  }

  .open .thinking-chevron {
    transform: rotate(90deg);
  }

  .thinking-label {
    font-weight: 500;
    color: var(--text-muted);
  }

  .thinking-label.streaming {
    font-style: italic;
    color: var(--text-secondary);
  }

  .thinking-dots {
    display: flex;
    gap: 3px;
    align-items: center;
  }

  .dot {
    width: 5px;
    height: 5px;
    background: var(--accent-primary);
    border-radius: 50%;
    animation: thinkingPulse 1.4s ease-in-out infinite;
  }

  .dot:nth-child(2) {
    animation-delay: 0.2s;
  }

  .dot:nth-child(3) {
    animation-delay: 0.4s;
  }

  @keyframes thinkingPulse {
    0%, 100% {
      opacity: 0.4;
      transform: scale(0.8);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  .thinking-content {
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    font-style: italic;
    animation: slideDown var(--transition-fast);
  }

  .thinking-content .markdown-content {
    opacity: 0.85;
    font-size: var(--font-size-sm);
  }

  @keyframes slideDown {
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
