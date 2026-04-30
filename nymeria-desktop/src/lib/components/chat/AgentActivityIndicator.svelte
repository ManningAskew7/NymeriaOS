<script lang="ts">
  import { onMount } from 'svelte';
  import type { Message, MessageStep } from '$lib/types';

  interface Props {
    message: Message;
  }

  type ActivityPhase = 'formulating' | 'waiting' | 'analyzing';
  type ToolStep = MessageStep & { type: 'tool_call' };

  const PHASE_TEXT: Record<ActivityPhase, string> = {
    formulating: 'Formulating tool calls',
    waiting: 'Waiting for tool call results',
    analyzing: 'Analyzing results',
  };

  let { message }: Props = $props();
  let pulse = $state(0);

  onMount(() => {
    const timer = setInterval(() => {
      pulse += 1;
    }, 420);

    return () => clearInterval(timer);
  });

  function isToolStep(step: MessageStep): step is ToolStep {
    return step.type === 'tool_call';
  }

  let phase = $derived.by((): ActivityPhase => {
    const steps = message.steps || [];
    const toolSteps = steps.filter(isToolStep);
    const runningTools = toolSteps.filter((step) => step.status === 'running');

    if (runningTools.length > 0) return 'waiting';
    if (toolSteps.length > 0) return 'analyzing';
    return 'formulating';
  });

  let dots = $derived('.'.repeat((pulse % 3) + 1));
</script>

<div class="activity-line" aria-label={`${PHASE_TEXT[phase]}...`}>
  <span class="activity-glyph" aria-hidden="true">
    <span class="glyph-core"></span>
    <span class="glyph-ring"></span>
  </span>
  {#key phase}
    <span class="activity-text" data-phase={phase}>
      {PHASE_TEXT[phase]}<span class="dots" aria-hidden="true">{dots}</span>
    </span>
  {/key}
</div>

<style>
  .activity-line {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) 0 2px;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.4;
    min-height: 28px;
  }

  .activity-glyph {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .glyph-core,
  .glyph-ring {
    position: absolute;
    border-radius: 50%;
  }

  .glyph-core {
    width: 6px;
    height: 6px;
    background: var(--accent-primary);
    box-shadow: var(--accent-glow-sm);
    animation: glyphPulse 1.45s ease-in-out infinite;
  }

  .glyph-ring {
    width: 14px;
    height: 14px;
    border: 1px solid color-mix(in srgb, var(--accent-primary) 55%, transparent);
    animation: glyphRing 1.45s ease-out infinite;
  }

  .activity-text {
    display: inline-block;
    min-width: 0;
    color: var(--text-secondary);
    animation: phaseSwap 240ms ease-out;
    white-space: nowrap;
  }

  @supports (-webkit-background-clip: text) {
    .activity-text {
      background: linear-gradient(
        100deg,
        var(--text-secondary) 0%,
        var(--text-secondary) 32%,
        color-mix(in srgb, var(--accent-primary) 85%, var(--text-primary)) 50%,
        var(--text-secondary) 68%,
        var(--text-secondary) 100%
      );
      background-size: 230% 100%;
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      animation: phaseSwap 240ms ease-out, textShine 2.1s ease-in-out infinite;
    }
  }

  .dots {
    display: inline-block;
    width: 1.1em;
    color: currentColor;
  }

  @keyframes glyphPulse {
    0%, 100% {
      opacity: 0.5;
      transform: scale(0.78);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  @keyframes glyphRing {
    0% {
      opacity: 0.65;
      transform: scale(0.68);
    }
    100% {
      opacity: 0;
      transform: scale(1.35);
    }
  }

  @keyframes phaseSwap {
    from {
      opacity: 0;
      transform: translateY(3px);
      filter: blur(1px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
      filter: blur(0);
    }
  }

  @keyframes textShine {
    0% { background-position: 120% 0; }
    100% { background-position: -120% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .glyph-core,
    .glyph-ring,
    .activity-text {
      animation: none;
    }
  }
</style>
