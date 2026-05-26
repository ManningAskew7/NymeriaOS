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
 */

import { cubicOut } from 'svelte/easing';

/**
 * Slide-open / slide-close parameters used by every dropdown, menu, popover,
 * and collapsible panel in the app.
 *
 * 120ms feels snappy without being abrupt; cubicOut keeps the close motion
 * decelerating into rest. `axis: 'y'` is correct for vertically-revealing
 * surfaces, which covers essentially every dropdown we render — surfaces that
 * need horizontal slide should declare their own params.
 */
export const DROPDOWN_TRANSITION = {
  duration: 120,
  easing: cubicOut,
  axis: 'y' as const,
};

/**
 * Longer slide for surfaces that reveal a large amount of content at once —
 * e.g. tool-call result panels inside a message bubble. The shorter
 * DROPDOWN_TRANSITION feels rapid when the body is many lines tall AND
 * triggers a visible reflow of the surrounding message bubble. This pace
 * keeps the bubble's height change smooth without dragging.
 */
export const TALL_DROPDOWN_TRANSITION = {
  duration: 260,
  easing: cubicOut,
  axis: 'y' as const,
};

