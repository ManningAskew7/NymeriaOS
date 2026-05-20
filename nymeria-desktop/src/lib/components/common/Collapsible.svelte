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
    display: flex;
    flex-direction: column;
  }

  .header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: 38px;
    padding: 0 var(--spacing-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    text-align: left;
    line-height: 1;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .open .header {
    border-bottom: none;
    border-radius: var(--radius-md) var(--radius-md) 0 0;
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
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-top: none;
    border-radius: 0 0 var(--radius-md) var(--radius-md);
    animation: slideDown var(--transition-fast);
    /* Brighten inherited text colors so muted/secondary text remains readable
       against the slightly lighter bubble background. */
    --text-muted: #a5abb3;
    --text-secondary: #c4c9d0;
    --border-subtle: #353a40;
    --border-default: #4a5058;
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
