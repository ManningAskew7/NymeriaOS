<script lang="ts">
  import type { AssistantActivityPhase, Message, MessageStep } from '$lib/types';
  import { chatStore } from '$lib/stores/chat.svelte';

  interface Props {
    message: Message;
  }

  type ToolStep = MessageStep & { type: 'tool_call' };

  const QUIET_TO_FORMULATING_MS = 1000;

  const PHASE_TEXT: Record<AssistantActivityPhase, string> = {
    processing: 'Processing',
    thinking: 'Thinking',
    typing: 'Processing',
    formulating: 'Formulating',
    compacting: 'Compacting',
    processing_results: 'Processing results',
    waiting: 'Waiting',
  };

  let { message }: Props = $props();
  let quietElapsed = $state(false);

  function isToolStep(step: MessageStep): step is ToolStep {
    return step.type === 'tool_call';
  }

  function inferPhaseFromSteps(): AssistantActivityPhase {
    const steps = message.steps || [];
    const toolSteps = steps.filter(isToolStep);
    const runningTools = toolSteps.filter((step) => step.status === 'running');
    const latestStep = steps[steps.length - 1];

    if (runningTools.length > 0) return 'waiting';
    if (latestStep?.type === 'thinking') return 'thinking';
    if (latestStep?.type === 'response' || (!latestStep && message.content)) return 'typing';
    if (toolSteps.length > 0) return 'processing_results';
    return 'processing';
  }

  $effect(() => {
    const updatedAt =
      message.activityUpdatedAt?.getTime() ?? message.timestamp.getTime();
    quietElapsed = false;
    const remaining = QUIET_TO_FORMULATING_MS - (Date.now() - updatedAt);
    if (remaining <= 0) {
      quietElapsed = true;
      return;
    }
    const handle = setTimeout(() => {
      quietElapsed = true;
    }, remaining);
    return () => clearTimeout(handle);
  });

  let phase = $derived.by((): AssistantActivityPhase => {
    // While the backend is compacting context, surface that explicitly instead
    // of the generic processing/formulating labels.
    if (chatStore.isCompacting) {
      return 'compacting';
    }
    const basePhase = message.activityPhase || inferPhaseFromSteps();
    if ((basePhase === 'processing' || basePhase === 'thinking') && quietElapsed) {
      return 'formulating';
    }
    return basePhase;
  });
</script>

{#if phase !== 'typing'}
  <div class="activity-line" aria-label={`${PHASE_TEXT[phase]}...`}>
    <span class="activity-text accent-wave-text" data-phase={phase}>
      {#key phase}
        <span class="phase-label">{PHASE_TEXT[phase]}</span>
      {/key}
      <span class="dots" aria-hidden="true">
        <span class="dot"></span>
        <span class="dot"></span>
        <span class="dot"></span>
      </span>
    </span>
  </div>
{/if}

<style>
  .activity-line {
    display: inline-flex;
    align-items: center;
    padding: var(--spacing-sm) 0 2px;
    font-size: var(--font-size-sm);
    line-height: 1.4;
    min-height: 28px;
  }

  .activity-text {
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
    min-width: 0;
    white-space: nowrap;
  }

  .phase-label {
    display: inline-block;
    animation: phaseSwap 280ms ease-out;
  }

  .dots {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    margin-left: 2px;
    /* Dots aren't text, so opt them out of the gradient text-clip. */
    -webkit-text-fill-color: initial;
  }

  .dot {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--accent-primary);
    animation: accentDotPulse 1.4s ease-in-out infinite;
  }

  .dot:nth-child(2) {
    animation-delay: 0.16s;
  }

  .dot:nth-child(3) {
    animation-delay: 0.32s;
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

  @media (prefers-reduced-motion: reduce) {
    .phase-label {
      animation: none;
    }
    .dot {
      animation: none;
      opacity: 0.6;
    }
  }
</style>
