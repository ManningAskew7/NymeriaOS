<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from './Icon.svelte';

  interface SectionAction {
    label: string;
    onClick: () => void;
  }

  interface Props {
    title: string;
    /** Count badge — rendered only when a number greater than 0 is passed. */
    count?: number | null;
    /** Trailing action link (e.g. "New task"); omit or pass null for none. */
    action?: SectionAction | null;
    /** When false, the chevron is dropped and the body is always shown. */
    collapsible?: boolean;
    defaultOpen?: boolean;
    children: Snippet;
  }

  let {
    title,
    count = null,
    action = null,
    collapsible = true,
    defaultOpen = true,
    children,
  }: Props = $props();

  // Stable per-instance ids so the collapse toggle can advertise aria-controls
  // and the body can label back. Uniqueness only needs to hold for one page
  // lifetime; crypto.randomUUID is available in every Tauri WebView.
  const _uid = crypto.randomUUID().slice(0, 8);
  const headerId = `section-header-${_uid}`;
  const contentId = `section-content-${_uid}`;

  // svelte-ignore state_referenced_locally — intentional one-time initialization
  let isOpen = $state(collapsible ? defaultOpen : true);

  const showCount = $derived(count != null && count > 0);

  function toggle() {
    isOpen = !isOpen;
  }
</script>

<div class="section" class:open={isOpen}>
  <div class="section-header">
    {#if collapsible}
      <button
        id={headerId}
        class="section-toggle"
        type="button"
        onclick={toggle}
        aria-expanded={isOpen}
        aria-controls={contentId}
      >
        <span class="chevron"><Icon name="chevronRight" size={16} /></span>
        <span class="section-title">{title}</span>
        {#if showCount}
          <span class="section-count">{count}</span>
        {/if}
      </button>
    {:else}
      <div id={headerId} class="section-toggle static">
        <span class="section-title">{title}</span>
        {#if showCount}
          <span class="section-count">{count}</span>
        {/if}
      </div>
    {/if}

    {#if action}
      <button
        class="section-action"
        type="button"
        onclick={action.onClick}
        aria-label={action.label}
        data-tooltip={action.label}
      >
        <Icon name="plus" size={16} />
      </button>
    {/if}
  </div>

  {#if isOpen}
    <div
      id={contentId}
      class="section-content"
      role="region"
      aria-labelledby={headerId}
      transition:slide={DROPDOWN_TRANSITION}
    >
      {@render children()}
    </div>
  {/if}
</div>

<style>
  /* Mirrors the right-panel Collapsible card chrome (bordered rounded box,
     elevated header, padded body) so Tasks/Triggers keep their existing look
     and Activity now matches it. This is a dedicated component rather than the
     shared Collapsible because the header carries a trailing action link as a
     SIBLING of the collapse toggle: Collapsible's header is a single button,
     and nesting a second button inside it is invalid (interactive-in-
     interactive). The border + radius + clip live on the stable wrapper so the
     bottom corners stay curved while the body slides (same trick Collapsible
     uses). */
  .section {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .section-header {
    display: flex;
    align-items: stretch;
    background: var(--bg-elevated-2);
  }

  .section-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
    /* 38px desktop visual; --touch-target-min is mobile-only (48px). */
    min-height: var(--touch-target-min, 38px);
    padding: 0 var(--spacing-md);
    background: transparent;
    border: none;
    color: var(--text-primary);
    text-align: left;
    line-height: 1;
    cursor: pointer;
    transition: background var(--transition-fast);
  }

  button.section-toggle:hover {
    background: var(--bg-hover);
  }

  /* Inset ring: the header is flush against the wrapper's clip, so an outset
     ring would be shaved on three sides (matches Collapsible). */
  .section-toggle:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .section-toggle.static {
    cursor: default;
  }

  .chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    transition: transform 120ms var(--ease-out);
  }

  .section.open .chevron {
    transform: rotate(90deg);
  }

  .section-title {
    font-weight: 600;
    font-size: var(--font-size-md);
    color: var(--text-primary);
    /* Reserve a fixed title width so the trailing count starts at the same x as
       the group counts below (see RightPanel's --count-col-label). The header
       prefix (16px toggle pad + 16px chevron + 8px gap) is 24px wider than a
       group label's prefix (8px content pad + 8px label pad), so the title's
       reserved width is the group column minus 24px. Falls back to 0 (no
       column) wherever --count-col-label is not set. */
    min-width: max(0px, calc(var(--count-col-label, var(--spacing-lg)) - var(--spacing-lg)));
  }

  .section-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 20px;
    padding: 0 6px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    background: transparent;
    color: var(--text-muted);
    border-radius: var(--radius-full);
    /* Sits right after the title inside the toggle (the toggle's 8px gap spaces
       it off the title) so the badge reads as "Tasks 2", not as a number
       floating at the panel's right edge. */
    flex-shrink: 0;
  }

  /* Icon-only "+" create button, pushed to the row's right edge by the toggle's
     flex:1. Muted at rest, accent on hover — one consistent treatment for every
     section's create action. The md side padding aligns the glyph to the
     header's content gutter (its right edge matches the toggle's md padding)
     and gives a full-height click target (the header stretches its children).
     The accessible name + hover hint come from aria-label / data-tooltip. */
  .section-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    padding: 0 var(--spacing-md);
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    transition: color var(--transition-fast);
  }

  .section-action:hover {
    color: var(--accent-primary);
  }

  .section-action:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .section-content {
    /* Vertical keeps the md rhythm. Horizontal is 8px so that, combined with
       each row's own 8px side padding, the rows' leading icons land 16px from
       the card edge: directly under the header chevron, which sits at the same
       16px via section-toggle's md padding. Group labels add a matching 8px so
       they share that 16px line too. */
    padding: var(--spacing-md) var(--spacing-sm);
    background: var(--bg-elevated-2);
    /* No border/radius here: the wrapper owns both and clips this body to the
       rounded shape so the bottom corners never snap (see .section). */
  }
</style>
