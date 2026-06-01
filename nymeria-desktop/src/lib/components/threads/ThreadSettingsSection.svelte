<script lang="ts">
  import type { Snippet } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from '$lib/components/common/Icon.svelte';

  /**
   * Card-style section used across the per-thread settings tabs. Quiet and
   * uniform: a subtle bordered card with a bold title and muted description.
   * No accent fills (accent is reserved for active states / toggles / focus).
   */
  interface Props {
    title: string;
    icon?: string;
    description?: string;
    count?: number | null;
    collapsible?: boolean;
    defaultOpen?: boolean;
    /** Tighter body padding for list-heavy sections (tools). */
    flush?: boolean;
    /** Right-aligned header content, rendered as a sibling of the toggle. */
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

<section class="ts-section">
  <div class="ts-head">
    {#if collapsible}
      <button class="ts-head-btn" type="button" onclick={() => (open = !open)} aria-expanded={open}>
        <span class="ts-chevron" class:open><Icon name="chevronRight" size={14} /></span>
        {#if icon}<span class="ts-icon"><Icon name={icon} size={14} /></span>{/if}
        <span class="ts-title-wrap">
          <span class="ts-title">{title}{#if count != null}<span class="ts-count">{count}</span>{/if}</span>
          {#if description}<span class="ts-desc">{description}</span>{/if}
        </span>
      </button>
    {:else}
      <div class="ts-head-static">
        {#if icon}<span class="ts-icon"><Icon name={icon} size={14} /></span>{/if}
        <span class="ts-title-wrap">
          <span class="ts-title">{title}{#if count != null}<span class="ts-count">{count}</span>{/if}</span>
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
  .ts-section:last-child {
    margin-bottom: 0;
  }

  .ts-head {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-md);
  }

  .ts-head-btn,
  .ts-head-static {
    display: flex;
    align-items: flex-start;
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
    margin: calc(-1 * var(--spacing-md));
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
  }
  .ts-head-btn:hover {
    background: var(--bg-hover);
  }

  .ts-chevron {
    display: inline-flex;
    margin-top: 1px;
    color: var(--text-muted);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
  }
  .ts-chevron.open {
    transform: rotate(90deg);
  }

  .ts-icon {
    display: inline-flex;
    margin-top: 1px;
    color: var(--text-muted);
  }

  .ts-title-wrap {
    display: flex;
    flex-direction: column;
    gap: 3px;
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
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-full);
  }

  .ts-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
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
