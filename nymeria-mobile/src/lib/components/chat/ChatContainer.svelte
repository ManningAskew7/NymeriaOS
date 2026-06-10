<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import MessageBubble from './MessageBubble.svelte';
  import ToolReloadIndicator from './ToolReloadIndicator.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';

  let containerRef = $state<HTMLDivElement | null>(null);
  let showCompactedEmpty = $derived((chatStore.contextStats?.compactionCount ?? 0) > 0);

  // §6 a11y / Crit 4 — screen-reader announcement for streaming AI output.
  // Token-by-token aria-live is a known anti-pattern (interrupts the user
  // every chunk). We instead announce STATE TRANSITIONS only — start/end of
  // a stream or compaction — and leave the settled message content as a
  // normal DOM block the SR user can navigate to and read at their own pace.
  // The assistant bubble carries aria-busy while streaming so AT silences
  // any browse-mode reads of mid-stream content.
  let streamStatusMessage = $state('');
  let wasStreaming = false;
  let wasCompacting = false;

  $effect(() => {
    const isStreaming = chatStore.isStreaming;
    const isCompacting = chatStore.isCompacting;

    if (isCompacting && !wasCompacting) {
      streamStatusMessage = 'Compacting thread context';
    } else if (!isCompacting && wasCompacting && !isStreaming) {
      streamStatusMessage = 'Thread context compacted';
    } else if (isStreaming && !wasStreaming) {
      streamStatusMessage = 'Assistant is responding';
    } else if (!isStreaming && wasStreaming) {
      streamStatusMessage = 'Response ready';
    }

    wasStreaming = isStreaming;
    wasCompacting = isCompacting;
  });

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
</script>

<!--
  Crit 4 — polite status region for assistant stream lifecycle.
  Visually hidden; AT-only. aria-atomic ensures the whole message is read
  rather than diffed token-by-token.
-->
<div class="sr-only" role="status" aria-live="polite" aria-atomic="true">
  {streamStatusMessage}
</div>

<div class="chat-container" bind:this={containerRef}>
  {#if chatStore.isLoadingHistory}
    <div class="loading-state">
      <Spinner size="lg" />
      <p>Loading thread…</p>
    </div>
  {:else if chatStore.messages.length === 0}
    <div class="empty-state">
      <div class="empty-icon">
        <Icon name="chat" size={48} />
      </div>
      <h2>{showCompactedEmpty ? 'Context compacted' : 'Start a thread'}</h2>
      <p>
        {showCompactedEmpty
          ? 'Older messages were summarized. Send a message to continue.'
          : 'Send a message to start a thread with Nymeria'}
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

<style>
  .chat-container {
    height: 100%;
    overflow-y: auto;
    padding: var(--spacing-md);
    overscroll-behavior-y: contain;
  }

  /* WCAG-recommended visually-hidden pattern. Kept in the accessibility
     tree (not display:none) so screen readers can pick it up. */
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
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
    padding: var(--spacing-lg);
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
  }

  .empty-state h2 {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-lg);
    font-weight: 600;
    color: var(--text-primary);
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
</style>
