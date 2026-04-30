<script lang="ts">
  import { onMount } from 'svelte';
  import type { Message, MessageStep } from '$lib/types';

  interface Props {
    message: Message;
  }

  type ActivityPhase = 'processing' | 'thinking' | 'typing' | 'formulating' | 'waiting';
  type ToolStep = MessageStep & { type: 'tool_call' };

  const PHASE_TEXT: Record<ActivityPhase, string> = {
    processing: 'Processing',
    thinking: 'Thinking',
    typing: 'Typing',
    formulating: 'Formulating tool calls',
    waiting: 'Waiting',
  };

  let { message }: Props = $props();
  let pulse = $state(0);
  let now = $state(Date.now());
  let lastVisibleText = $state('');
  let lastVisibleTextChangeAt = $state(Date.now());

  onMount(() => {
    const timer = setInterval(() => {
      pulse += 1;
      now = Date.now();
    }, 420);

    return () => clearInterval(timer);
  });

  function isToolStep(step: MessageStep): step is ToolStep {
    return step.type === 'tool_call';
  }

  function latestStepText(steps: MessageStep[]): string {
    for (let i = steps.length - 1; i >= 0; i -= 1) {
      const step = steps[i];
      if (step.type === 'response' || step.type === 'thinking') {
        return step.content || '';
      }
    }
    return message.content || '';
  }

  let visibleText = $derived(latestStepText(message.steps || []));

  $effect(() => {
    if (visibleText !== lastVisibleText) {
      lastVisibleText = visibleText;
      lastVisibleTextChangeAt = Date.now();
    }
  });

  let phase = $derived.by((): ActivityPhase => {
    const steps = message.steps || [];
    const toolSteps = steps.filter(isToolStep);
    const runningTools = toolSteps.filter((step) => step.status === 'running');
    const latestStep = steps[steps.length - 1];
    const textQuietMs = now - lastVisibleTextChangeAt;

    if (runningTools.length > 0) return 'waiting';
    if (latestStep?.type === 'thinking') {
      return textQuietMs < 1000 ? 'thinking' : 'formulating';
    }
    if (latestStep?.type === 'response' || (!latestStep && visibleText)) {
      return textQuietMs < 1000 ? 'typing' : 'formulating';
    }
    if (toolSteps.length > 0) return 'formulating';
    return 'processing';
  });

  let dots = $derived('.'.repeat((pulse % 3) + 1));
</script>

<div class="activity-line" aria-label={`${PHASE_TEXT[phase]}...`}>
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
    padding: var(--spacing-sm) 0 2px;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.4;
    min-height: 28px;
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
    .activity-text {
      animation: none;
    }
  }
</style>
