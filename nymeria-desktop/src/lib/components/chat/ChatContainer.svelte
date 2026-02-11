<script lang="ts">
  import { chatStore } from '$lib/stores/chat.svelte';
  import MessageBubble from './MessageBubble.svelte';

  let containerRef = $state<HTMLDivElement | null>(null);

  // Auto-scroll when new messages arrive or during streaming
  $effect(() => {
    if (chatStore.messages.length > 0 || chatStore.isStreaming) {
      scrollToBottom();
    }
  });

  function scrollToBottom() {
    if (containerRef) {
      // Use setTimeout to ensure DOM has updated
      setTimeout(() => {
        if (containerRef) {
          containerRef.scrollTop = containerRef.scrollHeight;
        }
      }, 0);
    }
  }
</script>

<div class="chat-container" bind:this={containerRef}>
  {#if chatStore.messages.length === 0}
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
      <h2>Start a conversation</h2>
      <p>Send a message to begin chatting with Nymeria</p>
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
    padding: var(--spacing-lg);
    scroll-behavior: smooth;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    text-align: center;
    color: var(--text-secondary);
  }

  .empty-icon {
    color: var(--accent-primary);
    opacity: 0.5;
    margin-bottom: var(--spacing-lg);
  }

  .empty-state h2 {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-xl);
    font-weight: 600;
    color: var(--text-primary);
  }

  .empty-state p {
    margin: 0;
    font-size: var(--font-size-base);
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
