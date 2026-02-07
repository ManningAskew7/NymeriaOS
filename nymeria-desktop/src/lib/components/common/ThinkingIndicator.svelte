<script lang="ts">
  interface Props {
    message?: string;
  }

  let { message = 'Thinking...' }: Props = $props();
</script>

<div class="thinking">
  <div class="dots">
    <span class="dot"></span>
    <span class="dot"></span>
    <span class="dot"></span>
  </div>
  <span class="message">{message}</span>
</div>

<style>
  .thinking {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .dots {
    display: flex;
    gap: 4px;
  }

  .dot {
    width: 6px;
    height: 6px;
    background: var(--accent-primary);
    border-radius: 50%;
    animation: pulse 1.4s ease-in-out infinite;
  }

  .dot:nth-child(2) {
    animation-delay: 0.2s;
  }

  .dot:nth-child(3) {
    animation-delay: 0.4s;
  }

  @keyframes pulse {
    0%,
    100% {
      opacity: 0.4;
      transform: scale(0.8);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  .message {
    font-style: italic;
  }
</style>
