<script module lang="ts">
  export interface SegmentedTab {
    id: string;
    label: string;
    /* Small leading badge (wizard step numbers). */
    badge?: string;
    /* Badge flips to a success fill (completed wizard steps). */
    complete?: boolean;
    disabled?: boolean;
    /* Stable DOM id when a tabpanel's aria-labelledby needs to point here. */
    domId?: string;
  }
</script>

<script lang="ts">
  interface Props {
    tabs: SegmentedTab[];
    active: string;
    onSelect: (id: string) => void;
    ariaLabel: string;
    /* Stretch the track to the container width with equal-width segments
       (sub-view switchers, wizard steps). Off = compact inline control
       sized to its labels (the dashboard header toggle). */
    fill?: boolean;
    /* id of the tabpanel this tablist controls (aria-controls). */
    controls?: string;
  }

  let { tabs, active, onSelect, ariaLabel, fill = false, controls }: Props = $props();

  let listEl = $state<HTMLDivElement>();

  // Roving tabindex: only one segment sits in the page tab order. Fall back
  // to the first enabled segment if the active one is disabled, so the
  // control never becomes keyboard-unreachable.
  const tabbableId = $derived(
    tabs.find((t) => t.id === active && !t.disabled)?.id
      ?? tabs.find((t) => !t.disabled)?.id
  );

  function handleKeydown(e: KeyboardEvent, tab: SegmentedTab) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const enabled = tabs.filter((t) => !t.disabled);
    if (enabled.length === 0) return;
    e.preventDefault();

    let next: SegmentedTab;
    if (e.key === 'Home') {
      next = enabled[0];
    } else if (e.key === 'End') {
      next = enabled[enabled.length - 1];
    } else {
      const dir = e.key === 'ArrowRight' ? 1 : -1;
      const i = Math.max(0, enabled.findIndex((t) => t.id === tab.id));
      next = enabled[(i + dir + enabled.length) % enabled.length];
    }

    // Automatic activation: selection follows focus (WAI-ARIA tabs pattern).
    if (next.id !== active) onSelect(next.id);
    requestAnimationFrame(() => {
      listEl
        ?.querySelector<HTMLElement>(`[data-tab-id="${CSS.escape(next.id)}"]`)
        ?.focus();
    });
  }
</script>

<div class="segmented" class:fill bind:this={listEl} role="tablist" aria-label={ariaLabel}>
  {#each tabs as tab (tab.id)}
    <button
      id={tab.domId}
      data-tab-id={tab.id}
      class="segment"
      class:active={active === tab.id}
      type="button"
      role="tab"
      aria-selected={active === tab.id}
      aria-controls={controls}
      disabled={tab.disabled}
      tabindex={tab.id === tabbableId ? 0 : -1}
      onclick={() => onSelect(tab.id)}
      onkeydown={(e) => handleKeydown(e, tab)}
    >
      {#if tab.badge}
        <span class="badge" class:done={tab.complete}>{tab.badge}</span>
      {/if}
      {tab.label}
    </button>
  {/each}
</div>

<style>
  /* One visibly outlined track groups the options; segments divide it with
     hairlines. The track border carries the "this is a control" signal, so
     inactive segments can stay quiet without becoming invisible. */
  .segmented {
    display: inline-flex;
    align-items: stretch;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    overflow: hidden;
    flex-shrink: 0;
  }

  .segmented.fill {
    display: flex;
  }

  .segmented.fill .segment {
    flex: 1 1 0;
  }

  .segment {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    white-space: nowrap;
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .segment + .segment {
    border-left: 1px solid var(--border-subtle);
  }

  .segment:hover:not(:disabled):not(.active) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  /* §3 — active segment is a tinted FILL, not just a border shift. */
  .segment.active {
    color: var(--accent-primary);
    background: var(--accent-tint-bg);
    font-weight: 600;
  }

  .segment:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .segment:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    border-radius: var(--radius-full);
    /* currentColor-derived so it tracks the segment's muted/accent text
       state without extra selectors. */
    background: color-mix(in srgb, currentColor 15%, transparent);
    font-size: var(--font-size-xs);
  }

  .badge.done {
    background: var(--success);
    color: var(--bg-base);
  }
</style>
