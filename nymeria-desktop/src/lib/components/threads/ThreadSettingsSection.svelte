<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from '$lib/components/common/Icon.svelte';

  /**
   * Card-style section used across every per-thread settings tab. Deliberately
   * NOT the global Settings panel's underlined-h3 / uppercase-small-caps look:
   * a rounded bordered card with an accent-tinted icon disc gives the thread
   * panel its own visual rhythm so the two surfaces never read as the same one.
   */
  interface Props {
    title: string;
    icon?: string;
    /** Short helper text rendered under the title. */
    description?: string;
    /** When set, renders a small count chip next to the title. */
    count?: number | null;
    collapsible?: boolean;
    defaultOpen?: boolean;
    /** Tighter body padding for list-heavy sections (tools). */
    flush?: boolean;
    /** Right-aligned header content (counts, buttons, etc.). Rendered as a
     *  sibling of the toggle button so interactive controls stay valid. */
    actions?: Snippet;
    children: Snippet;
  }

  let {
    title,
    icon,
    description,
    count = null,
    collapsible = false,
    defaultOpen = true,
    flush = false,
    actions,
    children,
  }: Props = $props();

  // svelte-ignore state_referenced_locally — intentional one-time initialization
  let open = $state(collapsible ? defaultOpen : true);
</script>

<section class="ts-section" class:collapsible>
  <div class="ts-section-head">
    {#if collapsible}
      <button
        class="ts-head-btn"
        type="button"
        onclick={() => (open = !open)}
        aria-expanded={open}
      >
        <span class="ts-chevron" class:open><Icon name="chevronRight" size={15} /></span>
        {#if icon}<span class="ts-icon"><Icon name={icon} size={15} /></span>{/if}
        <span class="ts-title-wrap">
          <span class="ts-title">
            {title}
            {#if count != null}<span class="ts-count">{count}</span>{/if}
          </span>
          {#if description}<span class="ts-desc">{description}</span>{/if}
        </span>
      </button>
    {:else}
      <div class="ts-head-static">
        {#if icon}<span class="ts-icon"><Icon name={icon} size={15} /></span>{/if}
        <span class="ts-title-wrap">
          <span class="ts-title">
            {title}
            {#if count != null}<span class="ts-count">{count}</span>{/if}
          </span>
          {#if description}<span class="ts-desc">{description}</span>{/if}
        </span>
      </div>
    {/if}

    {#if actions}
      <div class="ts-actions">{@render actions()}</div>
    {/if}
  </div>

  {#if open}
    <div class="ts-body" class:flush transition:slide={DROPDOWN_TRANSITION}>
      {@render children()}
    </div>
  {/if}
</section>

<style>
  .ts-section {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    margin-bottom: var(--spacing-md);
    overflow: hidden;
  }

  .ts-section-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
  }

  .ts-head-btn,
  .ts-head-static {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
    padding: 0;
    background: transparent;
    border: none;
    text-align: left;
    color: var(--text-primary);
    font: inherit;
  }

  .ts-head-btn {
    cursor: pointer;
    /* Stretch the hit target across the head row's padding. */
    margin: calc(-1 * var(--spacing-sm)) calc(-1 * var(--spacing-md));
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
  }
  .ts-head-btn:hover {
    background: var(--bg-hover);
  }

  .ts-chevron {
    display: inline-flex;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }
  .ts-chevron.open {
    transform: rotate(90deg);
  }

  /* Accent-tinted icon disc — a visual signature the global panel doesn't use. */
  .ts-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    flex-shrink: 0;
    border-radius: var(--radius-sm);
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
  }

  .ts-title-wrap {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    flex: 1;
  }

  .ts-title {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.3;
  }

  .ts-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
    border-radius: var(--radius-full);
  }

  .ts-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .ts-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin-left: auto;
    flex-shrink: 0;
  }

  .ts-body {
    padding: 0 var(--spacing-md) var(--spacing-md);
  }

  .ts-body.flush {
    padding: 0;
  }
</style>
