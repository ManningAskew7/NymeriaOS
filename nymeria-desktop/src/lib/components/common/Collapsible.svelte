<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
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
  // Active during the outro slide. While true the content's bottom border is
  // forced transparent so it doesn't visibly slide upward as the slide
  // transition shrinks the content's height (most noticeable on themes where
  // --border-subtle contrasts with the surrounding panel bg, e.g. Platinum).
  let isClosing = $state(false);

  function toggle() {
    isOpen = !isOpen;
    if (isOpen) isClosing = false;
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
    <div
      class="content"
      class:closing={isClosing}
      transition:slide={DROPDOWN_TRANSITION}
      onoutrostart={() => (isClosing = true)}
      onoutroend={() => (isClosing = false)}
    >
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
    /* Bottom border stays 1px wide in both states — only its color changes
       (visible when closed, transparent when open). Keeping the width
       constant avoids the layout shift / "flash" you get when style: none is
       toggled, since border-style isn't an animatable property. */
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), border-color 120ms ease-out, border-radius 120ms ease-out;
  }

  .open .header {
    border-bottom-color: transparent;
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
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
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
    /* Fade the bottom-border color during the outro so the 1px line doesn't
       visibly slide upward as the slide transition shrinks the content's
       height. Pairs with .closing below. */
    transition: border-bottom-color 120ms ease-out;
  }

  .content.closing {
    border-bottom-color: transparent;
  }
</style>
