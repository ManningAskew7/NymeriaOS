<script lang="ts">
  import { onMount } from 'svelte';
  import type { Message, MessageStep, ToolCallStatus } from '$lib/types';
  import { Icon } from '$lib/components/common';

  interface Props {
    message: Message;
  }

  type ActivityVariant = 'A' | 'B' | 'C';
  type ActivityPhase = 'formulating' | 'waiting' | 'analyzing' | 'reasoning';
  type ToolStep = MessageStep & { type: 'tool_call' };

  const VARIANT_KEY = 'nymeria-tool-activity-variant';

  const VARIANTS: { id: ActivityVariant; label: string; title: string }[] = [
    { id: 'A', label: 'A', title: 'Phase card' },
    { id: 'B', label: 'B', title: 'Phase card with tool summary' },
    { id: 'C', label: 'C', title: 'Compact tool stream preview' },
  ];

  const PHASE_COPY: Record<ActivityPhase, string[]> = {
    formulating: [
      'Formulating tool calls...',
      'Preparing tool arguments...',
      'Selecting the next action...',
    ],
    waiting: [
      'Waiting for tool call results...',
      'Collecting tool output...',
      'Letting the tools finish...',
    ],
    analyzing: [
      'Analyzing tool results...',
      'Checking what changed...',
      'Deciding the next step...',
    ],
    reasoning: [
      'Reasoning through the next step...',
      'Connecting the latest context...',
      'Working through the result...',
    ],
  };

  let { message }: Props = $props();
  let variant = $state<ActivityVariant>('B');
  let pulse = $state(0);

  onMount(() => {
    const saved = localStorage.getItem(VARIANT_KEY);
    if (saved === 'A' || saved === 'B' || saved === 'C') {
      variant = saved;
    }

    const timer = setInterval(() => {
      pulse += 1;
    }, 1000);

    return () => clearInterval(timer);
  });

  function setVariant(next: ActivityVariant) {
    variant = next;
    localStorage.setItem(VARIANT_KEY, next);
  }

  function isToolStep(step: MessageStep): step is ToolStep {
    return step.type === 'tool_call';
  }

  function displayToolName(name?: string): string {
    return name || 'tool';
  }

  function formatElapsed(startTime?: Date): string | null {
    if (!startTime) return null;
    const ms = Date.now() - new Date(startTime).getTime();
    if (!Number.isFinite(ms) || ms < 0) return null;
    const seconds = Math.max(1, Math.round(ms / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}m ${rest.toString().padStart(2, '0')}s`;
  }

  function statusLabel(status?: ToolCallStatus): string {
    switch (status) {
      case 'running':
        return 'running';
      case 'success':
        return 'done';
      case 'error':
        return 'error';
      case 'cancelled':
        return 'cancelled';
      default:
        return 'pending';
    }
  }

  function stringifyPreview(value: unknown, maxLength: number): string {
    if (value === undefined || value === null || value === '') return '';

    let text: string;
    if (typeof value === 'string') {
      text = value;
    } else {
      try {
        text = JSON.stringify(value);
      } catch {
        text = String(value);
      }
    }

    const compact = text.replace(/\s+/g, ' ').trim();
    if (compact.length <= maxLength) return compact;
    return `${compact.slice(0, maxLength - 1)}...`;
  }

  function toolPreview(step: ToolStep): { label: string; text: string } {
    if (step.result) {
      return {
        label: step.status === 'error' ? 'error' : 'result',
        text: stringifyPreview(step.result, 180),
      };
    }

    return {
      label: 'args',
      text: stringifyPreview(step.arguments || {}, 140) || 'waiting for arguments',
    };
  }

  let activity = $derived.by(() => {
    const steps = message.steps || [];
    const toolSteps = steps.filter(isToolStep);
    const runningTools = toolSteps.filter((step) => step.status === 'running');
    const latestStep = steps[steps.length - 1];
    const latestTool = toolSteps[toolSteps.length - 1];
    const completedTools = toolSteps.filter((step) => step.status === 'success').length;
    const erroredTools = toolSteps.filter((step) => step.status === 'error').length;
    const elapsedStart = runningTools[0]?.startTime || latestTool?.startTime || message.timestamp;

    let phase: ActivityPhase = 'formulating';
    let title = 'Preparing the next action';
    let detail = 'No tool output has arrived yet.';

    if (runningTools.length > 1) {
      phase = 'waiting';
      title = `Waiting for ${runningTools.length} tool results`;
      detail = `${toolSteps.length} tool calls in this turn.`;
    } else if (runningTools.length === 1) {
      phase = 'waiting';
      title = `Waiting for ${displayToolName(runningTools[0].name)}`;
      detail = 'Tool execution is in progress.';
    } else if (toolSteps.length === 0 && latestStep?.type === 'thinking') {
      phase = 'reasoning';
      title = 'Reasoning through the next step';
      detail = 'The model is producing reasoning content.';
    } else if (toolSteps.length === 0) {
      phase = 'formulating';
      title = 'Formulating tool calls';
      detail = 'The model has not emitted visible text yet.';
    } else if (latestTool?.status === 'error') {
      phase = 'analyzing';
      title = 'Reviewing tool error';
      detail = `Latest: ${displayToolName(latestTool.name)} failed.`;
    } else {
      phase = latestStep?.type === 'thinking' ? 'reasoning' : 'analyzing';
      title = phase === 'reasoning' ? 'Reasoning through the next step' : 'Analyzing tool results';
      detail = latestTool
        ? `Latest: ${displayToolName(latestTool.name)} ${statusLabel(latestTool.status)}.`
        : 'Checking the latest context.';
    }

    const copy = PHASE_COPY[phase][Math.floor(pulse / 3) % PHASE_COPY[phase].length];
    const stream = toolSteps.slice(-4).reverse();

    return {
      phase,
      title,
      detail,
      copy,
      toolSteps,
      runningTools,
      completedTools,
      erroredTools,
      latestTool,
      elapsed: formatElapsed(elapsedStart),
      stream,
    };
  });
</script>

<div class="activity-card" data-variant={variant}>
  <div class="activity-main">
    <div class="activity-pulse" aria-hidden="true">
      <span class="pulse-dot"></span>
      <span class="pulse-ring"></span>
    </div>

    <div class="activity-copy">
      <div class="activity-title-row">
        <span class="activity-title">{activity.title}</span>
        {#if activity.elapsed}
          <span class="elapsed">{activity.elapsed}</span>
        {/if}
      </div>
      <div class="phase-text">{activity.copy}</div>
    </div>

    <div class="variant-switcher" aria-label="Activity indicator variant">
      {#each VARIANTS as option (option.id)}
        <button
          type="button"
          class:active={variant === option.id}
          title={option.title}
          aria-pressed={variant === option.id}
          onclick={() => setVariant(option.id)}
        >
          {option.label}
        </button>
      {/each}
    </div>
  </div>

  {#if variant !== 'A'}
    <div class="tool-summary">
      <span class="summary-pill">
        <Icon name="terminal" size={13} />
        {activity.runningTools.length > 0
          ? `${activity.runningTools.length} running`
          : `${activity.toolSteps.length} called`}
      </span>
      {#if activity.completedTools > 0}
        <span class="summary-pill success">{activity.completedTools} done</span>
      {/if}
      {#if activity.erroredTools > 0}
        <span class="summary-pill error">{activity.erroredTools} error</span>
      {/if}
      {#if activity.latestTool}
        <span class="latest-tool" title={displayToolName(activity.latestTool.name)}>
          latest: {displayToolName(activity.latestTool.name)}
        </span>
      {:else}
        <span class="latest-tool">{activity.detail}</span>
      {/if}
    </div>
  {/if}

  {#if variant === 'C' && activity.stream.length > 0}
    <div class="tool-stream" aria-label="Recent tool call previews">
      {#each activity.stream as step (step.id || `${step.name}-${activity.stream.indexOf(step)}`)}
        {@const preview = toolPreview(step)}
        <div class="stream-row" data-status={step.status || 'pending'}>
          <span class="status-dot" aria-hidden="true"></span>
          <span class="stream-name">{displayToolName(step.name)}</span>
          <span class="stream-status">{statusLabel(step.status)}</span>
          <span class="stream-preview">
            <span class="preview-label">{preview.label}</span>
            {preview.text}
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .activity-card {
    position: relative;
    margin-top: var(--spacing-sm);
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--accent-primary) 28%, var(--glass-border));
    border-radius: var(--radius-md);
    background:
      linear-gradient(135deg, color-mix(in srgb, var(--accent-primary) 8%, transparent), transparent 55%),
      var(--glass-bg);
    color: var(--text-primary);
    overflow: hidden;
    isolation: isolate;
  }

  .activity-card::after {
    content: '';
    position: absolute;
    inset: 0;
    z-index: -1;
    background: linear-gradient(
      90deg,
      transparent 0%,
      transparent 26%,
      var(--accent-primary) 50%,
      transparent 74%,
      transparent 100%
    );
    opacity: 0.08;
    transform: translateX(-100%);
    animation: activityWave 2.4s ease-in-out infinite;
    pointer-events: none;
  }

  .activity-main {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  .activity-pulse {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    flex-shrink: 0;
  }

  .pulse-dot,
  .pulse-ring {
    position: absolute;
    border-radius: 50%;
  }

  .pulse-dot {
    width: 7px;
    height: 7px;
    background: var(--accent-primary);
    box-shadow: var(--accent-glow-sm);
    animation: activityPulse 1.5s ease-in-out infinite;
  }

  .pulse-ring {
    width: 18px;
    height: 18px;
    border: 1px solid color-mix(in srgb, var(--accent-primary) 55%, transparent);
    animation: activityRing 1.5s ease-out infinite;
  }

  .activity-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .activity-title-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    min-width: 0;
  }

  .activity-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .elapsed {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .phase-text {
    width: fit-content;
    max-width: 100%;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  @supports (-webkit-background-clip: text) {
    .phase-text {
      background: linear-gradient(
        100deg,
        var(--text-secondary) 0%,
        var(--text-secondary) 35%,
        color-mix(in srgb, var(--accent-primary) 85%, var(--text-primary)) 50%,
        var(--text-secondary) 65%,
        var(--text-secondary) 100%
      );
      background-size: 220% 100%;
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      animation: textShine 2.4s ease-in-out infinite;
    }
  }

  .variant-switcher {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg-elevated-2) 72%, transparent);
    flex-shrink: 0;
  }

  .variant-switcher button {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 18px;
    border-radius: 3px;
    color: var(--text-muted);
    font-size: 0.65rem;
    font-family: var(--font-mono);
    line-height: 1;
    cursor: pointer;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }

  .variant-switcher button:hover,
  .variant-switcher button.active {
    color: var(--text-primary);
    background: color-mix(in srgb, var(--accent-primary) 18%, transparent);
  }

  .tool-summary {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-sm);
    min-width: 0;
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .summary-pill {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    min-width: fit-content;
    padding: 2px 6px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--bg-elevated-2) 55%, transparent);
  }

  .summary-pill.success {
    color: var(--success);
  }

  .summary-pill.error {
    color: var(--error);
  }

  .latest-tool {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
  }

  .tool-stream {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: var(--spacing-sm);
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  .stream-row {
    display: grid;
    grid-template-columns: 8px minmax(70px, 0.5fr) auto minmax(110px, 1fr);
    align-items: center;
    gap: var(--spacing-xs);
    min-width: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-muted);
  }

  .stream-row[data-status='running'] .status-dot,
  .stream-row[data-status='pending'] .status-dot {
    background: var(--accent-primary);
    animation: activityPulse 1.4s ease-in-out infinite;
  }

  .stream-row[data-status='success'] .status-dot {
    background: var(--success);
  }

  .stream-row[data-status='error'] .status-dot {
    background: var(--error);
  }

  .stream-name,
  .stream-status,
  .stream-preview {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stream-name,
  .stream-status {
    font-family: var(--font-mono);
  }

  .stream-status {
    color: var(--text-muted);
  }

  .preview-label {
    margin-right: 4px;
    color: var(--accent-primary);
    font-family: var(--font-mono);
  }

  @keyframes activityWave {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
  }

  @keyframes activityPulse {
    0%, 100% {
      opacity: 0.55;
      transform: scale(0.85);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  @keyframes activityRing {
    0% {
      opacity: 0.75;
      transform: scale(0.65);
    }
    100% {
      opacity: 0;
      transform: scale(1.25);
    }
  }

  @keyframes textShine {
    0% { background-position: 120% 0; }
    100% { background-position: -120% 0; }
  }

  @media (max-width: 720px) {
    .activity-main {
      align-items: flex-start;
    }

    .activity-title-row,
    .tool-summary {
      flex-wrap: wrap;
    }

    .elapsed {
      margin-left: 0;
    }

    .stream-row {
      grid-template-columns: 8px minmax(64px, 0.55fr) auto minmax(80px, 1fr);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .activity-card::after,
    .pulse-dot,
    .pulse-ring,
    .stream-row[data-status='running'] .status-dot,
    .stream-row[data-status='pending'] .status-dot,
    .phase-text {
      animation: none;
    }
  }
</style>
