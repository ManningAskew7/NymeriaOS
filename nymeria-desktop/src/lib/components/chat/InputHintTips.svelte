<script lang="ts">
  import { untrack } from 'svelte';
  import { fade } from 'svelte/transition';

  interface Props {
    /** Pause rotation while the user is actively composing a message. */
    paused?: boolean;
  }

  let { paused = false }: Props = $props();

  // How long each tip stays on screen. ~9s reads comfortably for a short
  // sentence without feeling stale.
  const ROTATE_MS = 9000;

  const tips = [
    'Type / to browse slash commands.',
    'Use /skill <name> to load a skill for this thread.',
    'Use /kit <name> to bind a Skill Kit and its tools.',
    'Each thread keeps its own model, tools, and memory.',
    'Switch the model for a thread from its settings.',
    'Schedule a task and Nymeria will run it on its own.',
    'Set up triggers to start threads from email, RSS, webhooks, and HTTP polls.',
    'Give a thread its own custom instructions in settings.',
    'Create callable threads to hand work to a sub-agent.',
    'Click the dot to minimise the context usage above.',
    'Click any Global Dashboard item to open its thread.',
    'Collapse either sidebar to free up room.',
    'Skill Kits hot-load tools into the agent instantly.',
    'Nymeria helps providers cache prompts to cut costs.',
    'Nymeria can build new tools with tool_create mid-turn.',
    'Use @<title> to message another thread in place.',
    'Enable rag_search so Nymeria recalls details on demand.',
    'Set the compaction threshold by tokens, not percentage.',
    'Start a new thread when you switch tasks.',
    'Use cheap models per thread for repetitive tasks.',
    'Toggle dreaming to let a thread manage its own work.',
    'Group threads into a team to hide them from the rest.',
    'Use /orchestrate to spin up and manage an agent swarm.',
  ];

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
  title={current}
  onmouseenter={() => (hovered = true)}
  onmouseleave={() => (hovered = false)}
>
  <!-- L-shaped connector tying the tip to the prompt bar above, like the
       corner Claude Code draws under its input. The vertical leg rises by
       --prompt-stack-gap so it meets the bar's bottom-left edge. -->
  <span class="elbow" aria-hidden="true"></span>
  <span class="tip-clip">
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
    opacity: 0.75;
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
