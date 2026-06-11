/**
 * Shared animation constants.
 *
 * One source of truth so every dropdown / collapsible / menu opens and closes
 * with the same speed and easing. If you're adding a new dropdown-style
 * surface, import `DROPDOWN_TRANSITION` from here and pass it to
 * `transition:slide` — do NOT redefine the values inline.
 *
 * ```svelte
 * <script>
 *   import { slide } from 'svelte/transition';
 *   import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
 * </script>
 *
 * {#if open}
 *   <div transition:slide={DROPDOWN_TRANSITION}>…</div>
 * {/if}
 * ```
 *
 * The same applies to overlays: the in/out pairs below are the app's whole
 * overlay vocabulary (modals, sheets, pages, toasts). Pass them to `in:` /
 * `out:` directives — entrances ease out, exits ease in at roughly 60-70%
 * of the entrance duration, per the design guide.
 */

import { cubicIn, cubicOut, expoOut } from 'svelte/easing';

/**
 * OS-level "reduce motion" preference. Svelte transitions are JS-driven, so
 * the CSS `prefers-reduced-motion` floor in app.css cannot stop them; instead
 * the shared constants below collapse to zero duration when the preference is
 * on. `duration` is a getter so the preference is read each time a transition
 * actually runs (live, like a CSS media query), not once at module load.
 */
const reducedMotionQuery =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null;

/** True when the OS asks for reduced motion. Read at call time. */
export function prefersReducedMotion(): boolean {
  return reducedMotionQuery?.matches ?? false;
}

/**
 * Attach the reduced-motion-gated `duration` getter to transition params.
 * Every constant in this file is built through this helper so the whole
 * motion vocabulary honors the OS preference from one place.
 */
function gatedDuration<T extends object>(ms: number, params: T) {
  return {
    ...params,
    get duration(): number {
      return prefersReducedMotion() ? 0 : ms;
    },
  };
}

/**
 * Slide-open / slide-close parameters used by every dropdown, menu, popover,
 * and collapsible panel in the app.
 *
 * 120ms feels snappy without being abrupt; cubicOut keeps the close motion
 * decelerating into rest. `axis: 'y'` is correct for vertically-revealing
 * surfaces, which covers essentially every dropdown we render — surfaces that
 * need horizontal slide should declare their own params.
 */
export const DROPDOWN_TRANSITION = gatedDuration(120, {
  easing: cubicOut,
  axis: 'y' as const,
});

/**
 * Longer slide for surfaces that reveal a large amount of content at once —
 * e.g. tool-call result panels inside a message bubble. The shorter
 * DROPDOWN_TRANSITION feels rapid when the body is many lines tall AND
 * triggers a visible reflow of the surrounding message bubble. This pace
 * keeps the bubble's height change smooth without dragging.
 */
export const TALL_DROPDOWN_TRANSITION = gatedDuration(260, {
  easing: cubicOut,
  axis: 'y' as const,
});

/**
 * Tab-content swap: fade + 3px rise, the one recipe for every tab-pane
 * swap (RightPanel, ThreadList, SettingsPanel, ThreadSettingsPanel). Use
 * with `in:fly`; containers built to overlap their keyed panes (RightPanel,
 * ThreadList) also pass it to `out:fly` for a symmetric crossfade — don't
 * add `out:` where the container would stack the panes vertically instead.
 */
export const TAB_FADE = gatedDuration(160, { y: 3, easing: cubicOut });

/* ------------------------------------------------------------------------
 * Overlay vocabulary (§8 phase 2). Pairs are listed together; a surface
 * that uses DIALOG_RISE_IN must use DIALOG_RISE_OUT, and so on.
 * ------------------------------------------------------------------------ */

/** Backdrop / scrim fade-in. Use with `fade`. */
export const OVERLAY_FADE_IN = gatedDuration(150, {});

/**
 * Backdrop exit. Held at the panel exit's 160ms rather than a shorter fade:
 * the backdrop is the PARENT of the panel it scrims, and a parent reaching
 * opacity 0 early would visually cut off its child's outro mid-flight.
 */
export const OVERLAY_FADE_OUT = gatedDuration(160, { easing: cubicIn });

/** Centered dialog / floating panel: fade + 12px rise. Use with `fly`. */
export const DIALOG_RISE_IN = gatedDuration(250, { y: 12, easing: cubicOut });
export const DIALOG_RISE_OUT = gatedDuration(160, { y: 12, easing: cubicIn });

/** Image-viewer content: fade + slight scale. Use with `scale`. */
export const DIALOG_SCALE_IN = gatedDuration(150, { start: 0.95, easing: cubicOut });
export const DIALOG_SCALE_OUT = gatedDuration(100, { start: 0.95, easing: cubicIn });

/**
 * Mobile fullscreen page: slides in from the right and returns to the right
 * (spatial continuity with the header's back-chevron). Transform-only —
 * `opacity: 1` keeps the page solid while it travels. Use with `fly`.
 */
export const PAGE_SLIDE_IN = gatedDuration(250, { x: '100%', opacity: 1, easing: cubicOut });
export const PAGE_SLIDE_OUT = gatedDuration(180, { x: '100%', opacity: 1, easing: cubicIn });

/**
 * Mobile bottom sheet: full-height rise at the documented §7 spring pace
 * (220ms; expoOut is the JS stand-in for cubic-bezier(0.16, 1, 0.3, 1) —
 * the same fast-start, long-settle shape). Transform-only; the backdrop
 * carries the fade. Use with `fly`.
 */
export const SHEET_RISE_IN = gatedDuration(220, { y: '100%', opacity: 1, easing: expoOut });
export const SHEET_RISE_OUT = gatedDuration(160, { y: '100%', opacity: 1, easing: cubicIn });

/**
 * Toasts, with `fly`. Desktop's stack sits top-right and slides in from the
 * right; mobile's sits top-center and drops from above — each direction
 * matches its stack position (documented keep). Put TOAST_FLIP on the keyed
 * stack (`animate:flip`) so remaining toasts glide instead of snapping.
 */
export const TOAST_SLIDE_IN = gatedDuration(250, { x: 8, easing: cubicOut });
export const TOAST_SLIDE_OUT = gatedDuration(160, { x: 8, easing: cubicIn });
export const TOAST_DROP_IN = gatedDuration(250, { y: -8, easing: cubicOut });
export const TOAST_DROP_OUT = gatedDuration(160, { y: -8, easing: cubicIn });
export const TOAST_FLIP = gatedDuration(200, { easing: cubicOut });
