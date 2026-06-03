<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import { Icon } from '$lib/components/common';

  const COLLAPSE_KEY = 'nymeria_context_bar_collapsed';

  function readInitialCollapsed(): boolean {
    if (typeof localStorage === 'undefined') return false;
    return localStorage.getItem(COLLAPSE_KEY) === '1';
  }

  // svelte-ignore state_referenced_locally
  let isCollapsed = $state(readInitialCollapsed());

  function toggleCollapsed() {
    isCollapsed = !isCollapsed;
    if (typeof localStorage !== 'undefined') {
      if (isCollapsed) localStorage.setItem(COLLAPSE_KEY, '1');
      else localStorage.removeItem(COLLAPSE_KEY);
    }
  }

  function formatTokenCount(tokens: number): string {
    if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
    if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}k`;
    return `${tokens}`;
  }

  function getUsageColor(percentage: number): string {
    if (percentage >= 80) return 'var(--error)';
    if (percentage >= 50) return 'var(--warning)';
    return 'var(--success)';
  }

  // $derived so the template stays clean (Svelte 5 forbids root-level {@const}).
  // hasStats falls true the moment chatStore.contextStats arrives with a
  // non-zero token count — that's the trigger for chevron fade-in + bar
  // uncollapse on a fresh chat's first message.
  const stats = $derived(chatStore.contextStats);
  const hasStats = $derived(!!(stats && stats.totalTokens > 0));
  const usageColor = $derived(hasStats ? getUsageColor(stats!.usagePercentage) : 'transparent');
</script>

<!-- ContextStatusBar is ALWAYS rendered so the input section's height stays
     identical on a brand-new chat as on one with stats — no layout shift
     when the first message lands. Before any stats arrive (`hasStats`
     false) the bar carries the .no-stats class which forces its collapsed
     visual state AND hides the chevron via CSS (opacity 0). When the first
     `done` SSE event populates contextStats, .no-stats falls off → chevron
     fades in AND the bar uncollapses → .bar-content + ::before slide up
     from behind the prompt window via the existing local-collapse rules. -->
<div
  class="context-status-bar"
  class:collapsed={!hasStats || isCollapsed}
  class:no-stats={!hasStats}
>
  <!-- Toggle is positioned absolutely so it stays anchored at the
       left edge even when the rest of the bar slides down out of view.
       Rendered as a small green health-style dot instead of a chevron. -->
  <button
    class="collapse-toggle"
    type="button"
    onclick={toggleCollapsed}
    aria-label={isCollapsed ? 'Show context details' : 'Hide context details'}
    aria-expanded={!isCollapsed}
    aria-hidden={!hasStats}
    tabindex={hasStats ? 0 : -1}
    data-tooltip={isCollapsed ? 'Show context details' : 'Hide context details'}
  >
    <span class="toggle-dot" aria-hidden="true"></span>
  </button>
  <!-- bar-content holds the top divider + text. Slides DOWN as a single
       unit when the bar collapses, so both vanish "behind the prompt
       window" together. -->
  <div class="bar-content">
    {#if hasStats && stats}
      <div class="details">
        {#if chatStore.activeModel}
          <span class="model-name">{chatStore.activeModel}</span>
          <span class="separator">|</span>
        {/if}

        <span class="token-count">{formatTokenCount(stats.totalTokens)} tokens</span>
        <span class="separator">|</span>

        <span class="usage" style:color={usageColor}>
          {stats.usagePercentage}%
        </span>
        <div class="progress-bar">
          <div
            class="progress-fill"
            style:width="{Math.min(stats.usagePercentage, 100)}%"
            style:background={usageColor}
          ></div>
        </div>

        {#if stats.compactionCount > 0}
          <span class="separator">|</span>
          <span class="compaction-count" title="Times compacted">{stats.compactionCount}x compacted</span>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .context-status-bar {
    position: relative;
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    /* Transparent — the parent .input-section's sliding pseudo-element
       provides the surface colour. */
    background: transparent;
    user-select: none;
    flex-shrink: 0;
    /* Locked height — bar-content slides INSIDE this 20px box, and overflow
       hidden clips it as it leaves. */
    height: 20px;
    overflow: hidden;
  }

  /* Toggle — absolutely positioned at the top-left so it stays put while
     the rest of the bar (.bar-content) slides down out of view on collapse.
     Rendered as a small green dot instead of a chevron now. */
  .collapse-toggle {
    position: absolute;
    top: 3px;
    left: 2px;
    z-index: 2;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: 3px;
    cursor: pointer;
    /* Opacity transition uses the shared --sidebar-collapse-* vars so the
       dot's fade-in (when .no-stats falls off) stays in lock step with
       the bar + prompt. */
    transition: background 120ms ease,
                opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }

  .collapse-toggle:hover {
    background: var(--bg-hover);
  }

  /* Flat accent-tinted dot — no glow, no pulse. Picks up whichever
     --accent-primary the active theme defines. */
  .toggle-dot {
    display: block;
    flex: 0 0 auto;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-primary);
    transition: opacity 120ms ease;
    /* Nudged up 1px from the button's optical center — the button itself
       stays put at top:3px, only the dot moves. */
    transform: translateY(0);
  }

  /* Locally collapsed (or pre-stats) — dim the dot so the toggle's
     state still reads visually. */
  .context-status-bar.collapsed .toggle-dot {
    opacity: 0.45;
  }

  /* Before stats arrive (fresh chat with no messages), the chevron is
     invisible and click-inert. The collapse-toggle's existing opacity
     transition (declared in the .collapse-toggle base rule) carries it
     gracefully from 0 → 1 when the .no-stats class falls off as soon as
     the first `done` event populates contextStats. */
  .context-status-bar.no-stats .collapse-toggle {
    opacity: 0;
    pointer-events: none;
  }

  /* bar-content — carries the bar's BG, its top divider (border-top), AND
     the text. Slides down 100% of its height (= the bar's 20px) when
     collapsed, so the whole bar chrome (bg, top divider, text) vanishes
     together. The bottom divider is NOT here — it's the prompt window's
     top border and lives in MainPanel.svelte's .input-section pseudo. */
  .bar-content {
    position: relative;
    box-sizing: border-box;
    height: 100%;
    display: flex;
    align-items: center;
    /* 24px = 2px left padding + 16px chevron + 6px gap to text */
    padding-left: 24px;
    padding-right: var(--spacing-md);
    /* No bg or top border here — both are painted by MainPanel's
       .input-section::before, which spans the WHOLE section (bar + prompt
       window) as one sliding sheet. This component contributes only the
       text + chevron. */
    background: transparent;
    transform: translateY(0);
    opacity: 1;
    /* Uses the shared --sidebar-collapse-* vars so the bar slides in
       perfect sync with the prompt window's ::before pseudo when a
       sidebar collapses. The same transition also drives the local
       chevron-toggle collapse, so both gestures feel identical. */
    transition: transform var(--sidebar-collapse-duration) var(--sidebar-collapse-easing),
                opacity var(--sidebar-collapse-duration) var(--sidebar-collapse-easing);
  }

  .context-status-bar.collapsed .bar-content {
    transform: translateY(100%);
    opacity: 0;
    pointer-events: none;
  }

  .details {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs, 4px);
    /* 1px down + 3px left optical nudge for the whole text row. Composes
       with the per-element translateY(1px) below, so individual elements
       end up shifted by (-3px, 2px) total. */
    transform: translate(-3px, 1px);
  }

  /* Optical centering — every meaningful element (text + progress bar) sits
     1px below the line-box center; the separator pipes stay at center so they
     read as continuous vertical strokes. */
  .model-name {
    font-family: var(--font-mono);
    font-size: var(--font-size-3xs);
    opacity: 0.8;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transform: translateY(1px);
  }

  .separator {
    opacity: 0.5;
  }

  .token-count {
    font-family: var(--font-mono);
    font-size: var(--font-size-3xs);
    transform: translateY(1px);
  }

  .usage {
    font-family: var(--font-mono);
    font-size: var(--font-size-3xs);
    font-weight: 500;
    transform: translateY(1px);
  }

  .progress-bar {
    width: 40px;
    height: 3px;
    background: var(--border-subtle);
    border-radius: 2px;
    overflow: hidden;
    transform: translateY(1px);
  }

  .progress-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.3s ease, background 0.3s ease;
  }

  .compaction-count {
    font-size: 0.6rem;
    opacity: 0.7;
    transform: translateY(1px);
  }
</style>
