<script lang="ts">
  import { onMount } from 'svelte';

  let visible = $state(false);
  let text = $state('');
  let top = $state(0);
  let left = $state(0);
  let placement = $state<'top' | 'bottom'>('top');

  let currentTarget: HTMLElement | null = null;
  let showTimer: number | null = null;
  let tipEl: HTMLDivElement | null = $state(null);

  const SHOW_DELAY = 350;
  const MARGIN = 6;
  const EDGE_PAD = 4;

  function getTipText(el: HTMLElement): string {
    return el.getAttribute('data-tooltip') ?? '';
  }

  function reposition(target: HTMLElement) {
    if (!tipEl) return;
    const r = target.getBoundingClientRect();
    const pos = (target.getAttribute('data-tooltip-pos') as 'top' | 'bottom') || 'top';
    placement = pos;

    // First make sure it has correct size — we already updated text above,
    // so the DOM should have reflowed.
    const tr = tipEl.getBoundingClientRect();

    let nextTop: number;
    if (pos === 'bottom') {
      nextTop = r.bottom + MARGIN;
    } else {
      nextTop = r.top - tr.height - MARGIN;
    }
    let nextLeft = r.left + r.width / 2 - tr.width / 2;

    // Clamp to viewport
    nextLeft = Math.max(EDGE_PAD, Math.min(window.innerWidth - tr.width - EDGE_PAD, nextLeft));
    nextTop = Math.max(EDGE_PAD, Math.min(window.innerHeight - tr.height - EDGE_PAD, nextTop));

    top = nextTop;
    left = nextLeft;
  }

  function showFor(target: HTMLElement) {
    const t = getTipText(target);
    if (!t) return;
    text = t;
    visible = true;
    // Position after the DOM commits the new text
    requestAnimationFrame(() => {
      if (currentTarget === target) reposition(target);
    });
  }

  function scheduleShow(target: HTMLElement) {
    if (showTimer !== null) {
      window.clearTimeout(showTimer);
      showTimer = null;
    }
    // If already visible (user moved from one tooltipped element to another),
    // swap to the new one. Deferred to a microtask for the same reason as
    // hide(): onOver/onFocusIn can fire synchronously while Svelte is mid-flush
    // (focus relocating as DOM is inserted/removed), and showFor writes $state,
    // which would trip state_unsafe_mutation. The currentTarget recheck drops
    // the swap if hover/focus moved on before the microtask ran.
    if (visible) {
      queueMicrotask(() => {
        if (currentTarget === target) showFor(target);
      });
      return;
    }
    showTimer = window.setTimeout(() => {
      if (currentTarget === target) showFor(target);
    }, SHOW_DELAY);
  }

  function hide() {
    if (showTimer !== null) {
      window.clearTimeout(showTimer);
      showTimer = null;
    }
    currentTarget = null;
    if (!visible) return;
    // hide() runs from the global focus/mouse listeners, which can fire
    // synchronously while Svelte is mid-flush — e.g. a focused, tooltipped
    // element removed during another component's teardown dispatches
    // `focusout` during effect destruction. Writing the `visible` $state in
    // that window trips Svelte's state_unsafe_mutation guard, so defer the
    // write to a microtask (which runs after the flush completes). The
    // currentTarget check drops the deferred hide if a new anchor became
    // active in the same tick, so moving between adjacent tooltips doesn't
    // flicker off. Consequence: `visible` clears one microtask later, not
    // synchronously — don't assume it's false immediately after hide(). Safe
    // because this portal is a mounted singleton, so the microtask never
    // outlives the component except at app teardown (a harmless final write).
    queueMicrotask(() => {
      if (!currentTarget) visible = false;
    });
  }

  function findAnchor(e: Event): HTMLElement | null {
    const t = e.target as HTMLElement | null;
    if (!t || typeof t.closest !== 'function') return null;
    return t.closest('[data-tooltip]') as HTMLElement | null;
  }

  function onOver(e: MouseEvent) {
    const anchor = findAnchor(e);
    if (!anchor) return;
    if (anchor === currentTarget) return;
    currentTarget = anchor;
    scheduleShow(anchor);
  }

  function onOut(e: MouseEvent) {
    const anchor = findAnchor(e);
    if (!anchor || anchor !== currentTarget) return;
    const next = e.relatedTarget as Node | null;
    if (next && anchor.contains(next)) return;
    hide();
  }

  function onFocusIn(e: FocusEvent) {
    const anchor = findAnchor(e);
    if (!anchor) return;
    currentTarget = anchor;
    scheduleShow(anchor);
  }

  function onFocusOut(e: FocusEvent) {
    const anchor = findAnchor(e);
    if (!anchor || anchor !== currentTarget) return;
    hide();
  }

  function onAnyScroll() {
    hide();
  }

  onMount(() => {
    document.addEventListener('mouseover', onOver, true);
    document.addEventListener('mouseout', onOut, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('focusout', onFocusOut, true);
    window.addEventListener('scroll', onAnyScroll, true);
    window.addEventListener('resize', hide);
    window.addEventListener('blur', hide);
    document.addEventListener('keydown', hide);
    return () => {
      document.removeEventListener('mouseover', onOver, true);
      document.removeEventListener('mouseout', onOut, true);
      document.removeEventListener('focusin', onFocusIn, true);
      document.removeEventListener('focusout', onFocusOut, true);
      window.removeEventListener('scroll', onAnyScroll, true);
      window.removeEventListener('resize', hide);
      window.removeEventListener('blur', hide);
      document.removeEventListener('keydown', hide);
    };
  });
</script>

<div
  bind:this={tipEl}
  class="global-tooltip"
  class:visible
  style:top="{top}px"
  style:left="{left}px"
  role="tooltip"
  aria-hidden={!visible}
>
  {text}
</div>

<style>
  .global-tooltip {
    position: fixed;
    pointer-events: none;
    padding: 4px 8px;
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    font-weight: 400;
    line-height: 1.3;
    /* Short labels stay one line (box sizes to content under max-width); longer
       migrated tooltips (error text, disabled reasons, admin notes) wrap instead
       of overflowing the bubble. */
    white-space: normal;
    overflow-wrap: break-word;
    border-radius: var(--radius-sm);
    /* §7 — floating tooltip: shadow alone defines elevation; border
       would be redundant chrome. */
    box-shadow: var(--shadow-md);
    opacity: 0;
    transform: translateY(2px);
    transition: opacity var(--transition-fast), transform var(--transition-fast);
    /* Maximum safe stacking — nothing in the app uses a higher z-index. */
    z-index: 2147483647;
    top: 0;
    left: 0;
    max-width: 360px;
  }
  .global-tooltip.visible {
    opacity: 1;
    transform: translateY(0);
  }
</style>
