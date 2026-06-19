<script lang="ts">
  import type { Snippet } from 'svelte';
  import { tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    defaultOpen?: boolean;
    chevronIcon?: string;
    chevronSize?: number;
    /**
     * Override the slide transition params. Use TALL_DROPDOWN_TRANSITION (or a
     * custom slide config) for collapsibles whose body is large enough that
     * 120ms reads as a jump — e.g. tool-call cards inside message bubbles.
     */
    slideOptions?: typeof DROPDOWN_TRANSITION;
    header?: Snippet;
    children: Snippet;
  }

  let { title, defaultOpen = false, chevronIcon = 'chevronRight', chevronSize = 16, slideOptions = DROPDOWN_TRANSITION, header, children }: Props = $props();

  // Stable per-instance IDs so the header button can advertise aria-controls
  // pointing at the content panel, and the panel can label-back via
  // aria-labelledby. Uniqueness only needs to hold within a single page
  // lifetime; crypto.randomUUID is available in every WebView2/WKWebView Tauri
  // ships against.
  const _uid = crypto.randomUUID().slice(0, 8);
  const headerId = `collapsible-header-${_uid}`;
  const contentId = `collapsible-content-${_uid}`;

  // svelte-ignore state_referenced_locally — intentional one-time initialization
  let isOpen = $state(defaultOpen);

  let rootEl: HTMLDivElement;

  /**
   * If this Collapsible lives inside a .message-bubble, opening it can widen
   * the bubble (e.g. a Thought reveals a long single-line paragraph). Without
   * help, the bubble's width snaps to its new natural width the instant the
   * content is mounted, which reads as a jarring horizontal jump.
   *
   * This runs a FLIP-style width animation in lock step with the slide's
   * intro: snap the bubble back to its pre-open width without transition,
   * then transition it to its new natural width over the SAME duration as
   * the slide. Result: bubble grows horizontally + vertically together.
   *
   * Scoped to message-bubble parents only — other Collapsibles (right panel,
   * thread settings, etc.) keep their current behavior.
   *
   * NOTE: only runs on OPEN. Close currently snaps because measuring the
   * post-close narrow width requires the content to be unmounted (it's still
   * in DOM during the slide outro), and previous attempts to handle close
   * by pre-measuring with position:absolute interfered with the open path. */
  function animateBubbleWidth(oldWidth: number) {
    const bubble = rootEl?.closest('.message-bubble') as HTMLElement | null;
    if (!bubble || oldWidth === 0) return;
    const newWidth = bubble.offsetWidth;
    if (newWidth === oldWidth) return;

    bubble.style.transition = 'none';
    bubble.style.width = `${oldWidth}px`;
    void bubble.offsetWidth; // force reflow so the next frame starts at oldWidth

    const dur = slideOptions.duration ?? 120;
    requestAnimationFrame(() => {
      bubble.style.transition = `width ${dur}ms var(--ease-out)`;
      bubble.style.width = `${newWidth}px`;
    });
    setTimeout(() => {
      bubble.style.transition = '';
      bubble.style.width = '';
    }, dur + 40);
  }

  async function toggle() {
    // Capture the bubble's width BEFORE Svelte updates the DOM, so the FLIP
    // animation knows where to start from. Only meaningful when this
    // Collapsible is nested inside a .message-bubble, and only for the OPEN
    // direction (see animateBubbleWidth's note about close).
    const bubble = rootEl?.closest('.message-bubble') as HTMLElement | null;
    const oldWidth = bubble?.offsetWidth ?? 0;
    const wasOpen = isOpen;

    isOpen = !isOpen;

    // Only animate on OPEN. Close lets the slide outro run, content unmounts,
    // bubble snaps to narrow natural width.
    if (!wasOpen) {
      await tick();
      animateBubbleWidth(oldWidth);
    }
  }
</script>

<div class="collapsible" class:open={isOpen} bind:this={rootEl}>
  <button
    id={headerId}
    class="header"
    onclick={toggle}
    type="button"
    aria-expanded={isOpen}
    aria-controls={contentId}
  >
    <span class="chevron">
      <Icon name={chevronIcon} size={chevronSize} />
    </span>
    {#if header}
      {@render header()}
    {:else}
      <span class="title">{title}</span>
    {/if}
  </button>

  {#if isOpen}
    <div
      id={contentId}
      class="content"
      role="region"
      aria-labelledby={headerId}
      transition:slide={slideOptions}
    >
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .collapsible {
    display: flex;
    flex-direction: column;
    /* The rounded border + clip live on this stable wrapper, NOT on the header
       and content separately. Because the wrapper's radius never changes, the
       bottom corners stay curved at every frame as the body slides open or
       shut, instead of "snapping" from curved to straight the moment the
       header stops being the bottom of the box. (Standard fix for the
       accordion corner-snap artifact: round + clip on the parent rather than
       animating a child's corners.) Safe here because every Collapsible body
       is text/list content, with no pop-out menus that need to escape the clip. */
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    /* 38px is the desktop visual; --touch-target-min is defined only in the
       mobile app.css (48px), so this resolves to 38px on desktop (unchanged)
       and lifts the mobile tap target to the 48px floor (AI-UI §10). */
    min-height: var(--touch-target-min, 38px);
    padding: 0 var(--spacing-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    text-align: left;
    line-height: 1;
    transition: background var(--transition-fast);
  }

  /* The header is flush against the wrapper's clip, so an outset focus ring
     would be shaved off on three sides. Inset it (matches the edge-to-edge row
     treatment used elsewhere, e.g. ThreadItem / activity rows). */
  .header:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: -2px;
  }

  .header:hover {
    background: var(--bg-hover);
  }

  .chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    transition: transform 120ms var(--ease-out);
  }

  .open .chevron {
    transform: rotate(90deg);
  }

  .title {
    flex: 1;
    font-weight: 500;
  }

  .content {
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    /* No border or radius here: the wrapper owns both and clips this body to
       the rounded shape, so the bottom corners never snap (see .collapsible). */
  }
</style>
