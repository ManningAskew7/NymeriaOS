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
  // Active during the outro slide. While true the content's bottom border is
  // forced transparent so it doesn't visibly slide upward as the slide
  // transition shrinks the content's height (most noticeable on themes where
  // --border-subtle contrasts with the surrounding panel bg, e.g. Platinum).
  let isClosing = $state(false);

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
      bubble.style.transition = `width ${dur}ms cubic-bezier(0.33, 1, 0.68, 1)`;
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
    if (isOpen) isClosing = false;

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
      class:closing={isClosing}
      role="region"
      aria-labelledby={headerId}
      transition:slide={slideOptions}
      onoutrostart={() => (isClosing = true)}
      onoutroend={() => (isClosing = false)}
    >
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .collapsible {
    display: flex;
    flex-direction: column;
  }

  .header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: 38px;
    padding: 0 var(--spacing-md);
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    text-align: left;
    line-height: 1;
    /* Bottom border stays 1px wide in both states — only its color changes
       (visible when closed, transparent when open). Keeping the width
       constant avoids the layout shift / "flash" you get when style: none is
       toggled, since border-style isn't an animatable property. */
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    transition: background var(--transition-fast), border-color var(--transition-fast), border-radius var(--transition-fast);
  }

  .open .header {
    border-bottom-color: transparent;
    border-radius: var(--radius-md) var(--radius-md) 0 0;
  }

  .header:hover {
    background: var(--bg-hover);
  }

  .chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    transition: transform 120ms cubic-bezier(0.33, 1, 0.68, 1);
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
    border: 1px solid var(--border-subtle);
    border-top: none;
    border-radius: 0 0 var(--radius-md) var(--radius-md);
    /* Fade the bottom-border color during the outro so the 1px line doesn't
       visibly slide upward as the slide transition shrinks the content's
       height. Pairs with .closing below. */
    transition: border-bottom-color var(--transition-fast);
  }

  .content.closing {
    border-bottom-color: transparent;
  }
</style>
