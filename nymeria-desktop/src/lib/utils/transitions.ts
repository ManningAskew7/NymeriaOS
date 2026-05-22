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
