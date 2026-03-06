<script lang="ts">
  interface Props {
    text: string;
    isStreaming?: boolean;
  }

  let { text, isStreaming = false }: Props = $props();
</script>

<span class="streaming-text">
  {text}
  {#if isStreaming}
    <span class="cursor"></span>
  {/if}
</span>

<style>
  .streaming-text {
    white-space: pre-wrap;
    word-break: break-word;
  }

  .cursor {
    display: inline-block;
    width: 2px;
    height: 1.1em;
    background: var(--accent-primary);
    margin-left: 2px;
    vertical-align: text-bottom;
    animation: blink 0.8s ease-in-out infinite;
  }

  @keyframes blink {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0;
    }
  }
</style>
