<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import MessageBubble from './MessageBubble.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Spinner from '$lib/components/common/Spinner.svelte';

  let containerRef = $state<HTMLDivElement | null>(null);
  let showCompactedEmpty = $derived((chatStore.contextStats?.compactionCount ?? 0) > 0);

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

<div class="chat-container" bind:this={containerRef}>
  {#if chatStore.isLoadingHistory}
    <div class="loading-state">
      <Spinner size="lg" />
      <p>Loading conversation…</p>
    </div>
  {:else if chatStore.messages.length === 0}
    <div class="empty-state">
      <div class="empty-icon">
        <Icon name="chat" size={48} />
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
        <MessageBubble {message} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .chat-container {
    height: 100%;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: var(--spacing-md);
    overscroll-behavior-y: contain;
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
