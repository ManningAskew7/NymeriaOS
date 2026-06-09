<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import MessageBubble from './MessageBubble.svelte';
  import ToolReloadIndicator from './ToolReloadIndicator.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';

  let containerRef = $state<HTMLDivElement | null>(null);
  let showCompactedEmpty = $derived((chatStore.contextStats?.compactionCount ?? 0) > 0);
  let isScrolledUp = $state(false);
  const SCROLL_BUTTON_THRESHOLD = 160;

  // Auto-scroll when new messages arrive or during streaming
  $effect(() => {
    if (chatStore.messages.length > 0 || chatStore.isStreaming) {
      if (chatStore.instantScroll) {
        chatStore.setInstantScroll(false);
        scrollToBottom('instant');
      } else {
        scrollToBottom('smooth');
      }
    }
  });

  let scrollRafPending = false;

  function scrollToBottom(behavior: ScrollBehavior = 'smooth') {
    if (!containerRef) return;

    // Instant scroll always wins — bypass the rAF dedup guard so a pending
    // smooth scroll from the previous thread can't swallow it.
    if (behavior === 'instant') {
      scrollRafPending = false;
    }

    if (!scrollRafPending) {
      scrollRafPending = true;
      requestAnimationFrame(() => {
        scrollRafPending = false;
        if (containerRef) {
          containerRef.scrollTo({ top: containerRef.scrollHeight, behavior });
        }
      });
    }
  }

  function onScroll() {
    if (!containerRef) return;
    const distance = containerRef.scrollHeight - containerRef.scrollTop - containerRef.clientHeight;
    isScrolledUp = distance > SCROLL_BUTTON_THRESHOLD;
  }
</script>

<div class="chat-wrap">
<div class="chat-container" bind:this={containerRef} onscroll={onScroll}>
  {#if chatStore.isLoadingHistory}
    <div class="loading-state">
      <Spinner size="lg" />
      <p>Loading conversation…</p>
    </div>
  {:else if chatStore.messages.length === 0}
    <div class="empty-state">
      <div class="empty-icon">
        <svg
          width="64"
          height="64"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <path
            d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"
          />
        </svg>
      </div>
      <h2>{showCompactedEmpty ? 'Context compacted' : 'Start a conversation'}</h2>
      <p>
        {showCompactedEmpty
          ? 'Older messages were summarized. Send a message to continue.'
          : 'Send a message to begin chatting with Nymeria'}
      </p>
    </div>
  {:else}
    <div class="messages">
      {#each chatStore.messages as message (message.id)}
        {#if message.toolReloadInfo}
          <ToolReloadIndicator info={message.toolReloadInfo} />
        {/if}
        <MessageBubble {message} />
      {/each}
    </div>
  {/if}
</div>

<button
  type="button"
  class="jump-to-bottom"
  class:visible={isScrolledUp}
  onclick={() => scrollToBottom('smooth')}
  aria-label="Jump to latest message"
  tabindex={isScrolledUp ? 0 : -1}
>
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="m6 9 6 6 6-6" />
  </svg>
</button>
</div>

<style>
  .chat-wrap {
    position: relative;
    height: 100%;
    min-height: 0;
  }

  .chat-container {
    height: 100%;
    overflow-y: auto;
    padding: var(--spacing-lg);
    /* scroll-behavior handled programmatically via scrollToBottom() */
  }

  .jump-to-bottom {
    position: absolute;
    right: 18px;
    bottom: 14px;
    width: 32px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    background: var(--bg-elevated-2);
    border-radius: 50%;
    color: var(--text-secondary);
    cursor: pointer;
    /* §7 — floating button: shadow alone defines elevation; border
       would be redundant chrome on a small floating action button. */
    box-shadow: var(--shadow-md);
    opacity: 0;
    transform: translateY(8px);
    pointer-events: none;
    transition: opacity 160ms ease, transform 160ms ease, color 120ms ease, background 120ms ease, border-color 120ms ease;
    z-index: 5;
  }
  .jump-to-bottom.visible {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
  }
  .jump-to-bottom:hover {
    color: var(--accent-primary);
    border-color: color-mix(in srgb, var(--accent-primary) 45%, transparent);
    background: var(--bg-elevated);
  }

  .empty-state,
  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    text-align: center;
    color: var(--text-secondary);
    gap: var(--spacing-md);
  }

  .empty-state {
    animation: fadeIn 600ms ease-out;
  }

  .loading-state p {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .empty-icon {
    color: var(--accent-primary);
    opacity: 0.4;
    margin-bottom: var(--spacing-lg);
    filter: drop-shadow(0 0 12px var(--accent-tint-bg));
  }

  .empty-state h2 {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-xl);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .empty-state p {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .messages {
    display: flex;
    flex-direction: column;
    min-height: 100%;
  }

  @keyframes slideUp {
    from {
      opacity: 0;
      transform: translateY(10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
