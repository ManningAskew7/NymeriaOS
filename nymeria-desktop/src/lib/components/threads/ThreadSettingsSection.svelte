<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from '$lib/components/common/Icon.svelte';

  /**
   * Section primitive for the per-thread settings tabs. Mirrors the app's
   * shared settings idiom (an underlined `.section-heading` with a muted hint
   * and a plain body) rather than a bespoke card, so the panel keeps the global
   * menu's visual language and only its layout (top tabs + sub-tabs) differs.
   */
  interface Props {
    title: string;
    description?: string;
    count?: number | null;
    collapsible?: boolean;
    defaultOpen?: boolean;
    /** Tighter top gap for list-heavy sections (tools). */
    flush?: boolean;
    /**
     * Open state. Bindable so a parent can hoist it (and have it survive this
     * component being unmounted/remounted). Defaults from defaultOpen when
     * collapsible; a non-collapsible section is always open.
     */
    open?: boolean;
    children: Snippet;
  }

  let {
    title,
    description,
    count = null,
    collapsible = false,
    defaultOpen = true,
    flush = false,
    open = $bindable(collapsible ? defaultOpen : true),
    children,
  }: Props = $props();
</script>

<section class="ts-section">
  {#if collapsible}
    <button class="ts-head" type="button" onclick={() => (open = !open)} aria-expanded={open}>
      <span class="ts-chevron" class:open><Icon name="chevronRight" size={13} /></span>
      <span class="ts-heading">{title}</span>
      {#if count != null}<span class="ts-count">{count}</span>{/if}
    </button>
  {:else}
    <div class="ts-head">
      <span class="ts-heading">{title}</span>
      {#if count != null}<span class="ts-count">{count}</span>{/if}
    </div>
  {/if}

  {#if description}<p class="ts-desc">{description}</p>{/if}

  {#if open}
    <div class="ts-body" class:flush transition:slide={DROPDOWN_TRANSITION}>
      {@render children()}
    </div>
  {/if}
</section>

<style>
  .ts-section {
    margin-bottom: var(--spacing-lg);
  }
  .ts-section:last-child {
    margin-bottom: 0;
  }

  /* Underlined heading row, matching the global settings `.section-heading`. */
  .ts-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    width: 100%;
    margin: 0;
    padding: 0 0 var(--spacing-xs);
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--border-subtle);
    text-align: left;
    font: inherit;
    color: var(--text-primary);
  }

  button.ts-head {
    cursor: pointer;
  }
  button.ts-head:hover .ts-heading {
    color: var(--accent-primary);
  }

  .ts-chevron {
    display: inline-flex;
    color: var(--text-muted);
    transition: transform 120ms var(--ease-out);
  }
  .ts-chevron.open {
    transform: rotate(90deg);
  }

  .ts-heading {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.3;
  }

  /* Subtle running count, not a filled chip. */
  .ts-count {
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
  }

  .ts-desc {
    margin: var(--spacing-xs) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
  }

  .ts-body {
    margin-top: var(--spacing-md);
  }
  .ts-body.flush {
    margin-top: var(--spacing-sm);
  }
</style>
