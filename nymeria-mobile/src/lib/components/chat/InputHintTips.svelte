<script lang="ts">
  import { untrack } from 'svelte';
  import { fade } from 'svelte/transition';
  import { INPUT_TIPS } from '$lib/utils/inputTips';

  interface Props {
    /** Pause rotation while the user is actively composing a message. */
    paused?: boolean;
  }

  let { paused = false }: Props = $props();

  // How long each tip stays on screen. ~9s reads comfortably for a short
  // sentence without feeling stale.
  const ROTATE_MS = 9000;

  // Copy lives in the shared EXACT_MATCH data file; mobile renders the
  // non-desktopOnly subset. Simplifications vs the desktop component: no
  // hover pause (touch), no clip tooltip (mobile carries no tooltip
  // action), no elbow connector.
  const tips = INPUT_TIPS.filter((t) => !t.desktopOnly).map((t) => t.text);

  // Start somewhere random so the first tip varies between sessions, then
  // cycle through in order.
  let index = $state(Math.floor(Math.random() * 1_000_000));

  let current = $derived(tips[index % tips.length] ?? '');

  $effect(() => {
    // Re-evaluates when paused toggles or the tip set changes.
    if (paused || tips.length <= 1) return;
    const timer = setInterval(() => {
      // Don't burn rotations while the app is in the background; the user
      // can't see them and would miss tips on return.
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      index = (untrack(() => index) + 1) % tips.length;
    }, ROTATE_MS);
    return () => clearInterval(timer);
  });
</script>

<div class="tip" role="status" aria-live="off">
  {#key current}
    <span class="tip-text" in:fade={{ duration: 220 }}>{current}</span>
  {/key}
</div>

<style>
  .tip {
    min-width: 0;
    padding: 0 var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: left;
    /* Keep a single line; long tips ellipsize rather than reflow the bar. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tip-text {
    display: inline-block;
  }
</style>
