<script lang="ts">
  import { untrack } from 'svelte';
  import { fade } from 'svelte/transition';
  import { tooltipWhenClipped } from '$lib/actions/tooltip';
  import { INPUT_TIPS } from '$lib/utils/inputTips';

  interface Props {
    /** Pause rotation while the user is actively composing a message. */
    paused?: boolean;
  }

  let { paused = false }: Props = $props();

  // How long each tip stays on screen. ~9s reads comfortably for a short
  // sentence without feeling stale.
  const ROTATE_MS = 9000;

  // Copy lives in the shared EXACT_MATCH data file (mobile renders the
  // non-desktopOnly subset); this component owns only the presentation.
  const tips = INPUT_TIPS.map((t) => t.text);

  // Start somewhere random so the first tip varies between sessions, then
  // cycle through in order.
  let index = $state(Math.floor(Math.random() * 1_000_000));
  let hovered = $state(false);

  let current = $derived(tips[index % tips.length] ?? '');

  $effect(() => {
    // Re-evaluates when paused/hovered toggle or the tip set changes.
    if (paused || hovered || tips.length <= 1) return;
    const timer = setInterval(() => {
      // Don't burn rotations while the window is in the background; the user
      // can't see them and would miss tips on return.
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      index = (untrack(() => index) + 1) % tips.length;
    }, ROTATE_MS);
    return () => clearInterval(timer);
  });
</script>

<div
  class="tip"
  role="status"
  aria-live="off"
  onmouseenter={() => (hovered = true)}
  onmouseleave={() => (hovered = false)}
>
  <!-- L-shaped connector tying the tip to the prompt bar above, like the
       corner Claude Code draws under its input. The vertical leg rises by
       --prompt-stack-gap so it meets the bar's bottom-left edge. -->
  <span class="elbow" aria-hidden="true"></span>
  <span class="tip-clip" use:tooltipWhenClipped={current}>
    {#key current}
      <span class="tip-text" in:fade={{ duration: 220 }}>{current}</span>
    {/key}
  </span>
</div>

<style>
  .tip {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
    /* Shift the whole tip (elbow + text) right without altering either. */
    margin: 0 0 0 23px;
    padding-left: 18px;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: left;
  }

  .elbow {
    position: absolute;
    left: 2px;
    /* Corner sits at the tip text's vertical middle so the arm meets the
       text, while the leg still runs the full gap up to the prompt bar. */
    bottom: 8px;
    /* Rise up through the gap above so the leg meets the prompt bar's
       bottom-left. Falls back to 14px if the gap variable isn't in scope. */
    top: calc(-1 * var(--prompt-stack-gap, 14px));
    width: 9px;
    border-left: 1.5px solid var(--border-default);
    border-bottom: 1.5px solid var(--border-default);
    border-bottom-left-radius: 6px;
    pointer-events: none;
  }

  .tip-clip {
    display: block;
    min-width: 0;
    /* Match the keyboard hint's text exactly. */
    font-size: var(--font-size-xs);
    /* Nudge the text up and right so it tucks against the elbow's arm. */
    transform: translate(3px, -3px);
    /* Keep a single line; long tips ellipsize rather than reflow the bar. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tip-text {
    display: inline-block;
  }
</style>
