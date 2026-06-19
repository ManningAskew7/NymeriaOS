import type { Action } from 'svelte/action';

/**
 * tooltipWhenClipped — surface the custom tooltip ONLY when the element's text
 * is actually overflowing its box.
 *
 * The global portal (common/TooltipPortal.svelte) shows a styled bubble for any
 * element carrying a `data-tooltip` attribute on hover/focus. This action adds
 * that attribute when the node is clipped (horizontal ellipsis or vertical
 * line-clamp) and removes it when the text fits, so a "show the full text"
 * tooltip never repeats text that is already fully visible.
 *
 * Pass the full (untruncated) text as the parameter. Re-measures on resize
 * (ResizeObserver) and whenever the text changes (the `update` hook). Mirrors
 * the clamp-overflow detection used in dashboard/ActivityItem.svelte.
 */
export const tooltipWhenClipped: Action<HTMLElement, string | null | undefined> = (
  node,
  text,
) => {
  let current = text ?? '';

  function measure() {
    const clipped =
      node.scrollWidth - node.clientWidth > 1 || node.scrollHeight - node.clientHeight > 1;
    if (clipped && current) {
      node.setAttribute('data-tooltip', current);
    } else {
      node.removeAttribute('data-tooltip');
    }
  }

  // ResizeObserver fires once on observe, so the first real (post-layout)
  // measurement happens even when the synchronous call below sees a 0x0 box.
  measure();
  const ro = new ResizeObserver(measure);
  ro.observe(node);

  return {
    update(next) {
      current = next ?? '';
      measure();
    },
    destroy() {
      ro.disconnect();
      node.removeAttribute('data-tooltip');
    },
  };
};
