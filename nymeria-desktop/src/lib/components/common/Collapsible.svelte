<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    defaultOpen?: boolean;
    chevronIcon?: string;
    chevronSize?: number;
    header?: Snippet;
    children: Snippet;
  }

  let { title, defaultOpen = false, chevronIcon = 'chevronRight', chevronSize = 16, header, children }: Props = $props();

  // svelte-ignore state_referenced_locally — intentional one-time initialization
  let isOpen = $state(defaultOpen);

  function toggle() {
    isOpen = !isOpen;
  }
</script>

<div class="collapsible" class:open={isOpen}>
  <button class="header" onclick={toggle} type="button">
    <span class="chevron">
      <Icon name={chevronIcon} size={chevronSize} />
    </span>
    {#if header}
      {@render header()}
    {:else}
      <span class="title">{title}</span>
    {/if}
  </button>

  {#if isOpen}
    <div class="content">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .collapsible {
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    text-align: left;
    transition: background var(--transition-fast);
  }

  .header:hover {
    background: var(--bg-hover);
  }

  .chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    transition: transform var(--transition-normal) cubic-bezier(0.4, 0, 0.2, 1);
  }

  .open .chevron {
    transform: rotate(90deg);
  }

  .title {
    flex: 1;
    font-weight: 500;
  }

  .content {
    padding: var(--spacing-md);
    background: var(--bg-elevated);
    border-top: 1px solid var(--border-subtle);
    animation: slideDown var(--transition-fast);
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
