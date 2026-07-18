<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type { ServerSettings, ServerSettingsUpdate } from '$lib/types';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    browserTimezone,
    buildContextUpdate,
    contextStrategyFromSettings,
    type ContextStrategy,
  } from '$lib/utils/onboardingSetup';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    settings: ServerSettings | null;
    refresh: () => Promise<void>;
  }

  let { settings, refresh }: Props = $props();

  // Mirrors the CLI context step's CONTEXT_CHOICES.
  const STRATEGY_OPTIONS: { value: ContextStrategy; label: string; hint: string }[] = [
    {
      value: 'compact_tokens',
      label: 'Compact at a token count',
      hint: 'Summarize once the conversation reaches an absolute token size. Recommended.',
    },
    {
      value: 'compact_percent',
      label: 'Compact at a context percentage',
      hint: 'Summarize once a share of the model context window is used.',
    },
    {
      value: 'sliding_window',
      label: 'Sliding window',
      hint: 'Keep only the most recent turns. Legacy strategy.',
    },
    {
      value: 'none',
      label: 'None',
      hint: 'Never manage context automatically. Long threads eventually overflow.',
    },
  ];

  let strategy = $state<ContextStrategy>('compact_tokens');
  let thresholdTokens = $state('');
  let thresholdPercent = $state('');
  let windowCycles = $state('');
  let memoryMaxEntries = $state('');
  let memoryCharLimit = $state('');
  let memoryValueMaxChars = $state('');
  let toolOutputMaxChars = $state('');
  let toolTimeout = $state('');
  let timezone = $state('');

  let saveStatus = $state<'idle' | 'saving' | 'success' | 'error'>('idle');
  let saveMessage = $state('');
  let initializedFor = $state<ServerSettings | null>(null);

  const detectedTimezone = browserTimezone();

  $effect(() => {
    if (!settings || settings === initializedFor) return;
    initializedFor = settings;
    strategy = contextStrategyFromSettings(settings);
    thresholdTokens = String(settings.compact_threshold_tokens ?? 200000);
    thresholdPercent = String(Math.round((settings.compact_threshold ?? 0.8) * 100));
    windowCycles = String(settings.sliding_window_cycles ?? 5);
    memoryMaxEntries = String(settings.memory_max_entries ?? 100);
    memoryCharLimit = String(settings.memory_char_limit ?? 8000);
    memoryValueMaxChars = String(settings.memory_value_max_chars ?? 1000);
    toolOutputMaxChars = String(settings.tool_output_max_chars ?? 100000);
    toolTimeout = String(settings.tool_timeout ?? 300);
    timezone = settings.user_timezone ?? '';
  });

  function numberOrUndefined(raw: string): number | undefined {
    const trimmed = raw.trim();
    if (!trimmed) return undefined;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : undefined;
  }

  async function handleSave() {
    saveStatus = 'saving';
    saveMessage = '';
    try {
      const updates: ServerSettingsUpdate = buildContextUpdate(strategy, {
        tokens: numberOrUndefined(thresholdTokens),
        percent: numberOrUndefined(thresholdPercent),
        cycles: numberOrUndefined(windowCycles),
      });
      const entries = numberOrUndefined(memoryMaxEntries);
      if (entries !== undefined) updates.memory_max_entries = entries;
      const chars = numberOrUndefined(memoryCharLimit);
      if (chars !== undefined) updates.memory_char_limit = chars;
      const valueChars = numberOrUndefined(memoryValueMaxChars);
      if (valueChars !== undefined) updates.memory_value_max_chars = valueChars;
      const toolChars = numberOrUndefined(toolOutputMaxChars);
      if (toolChars !== undefined) updates.tool_output_max_chars = toolChars;
      const timeout = numberOrUndefined(toolTimeout);
      if (timeout !== undefined) updates.tool_timeout = timeout;
      if (timezone.trim() && timezone.trim() !== (settings?.user_timezone ?? '')) {
        updates.user_timezone = timezone.trim();
      }

      const result = await api.updateServerSettings(updates);
      saveStatus = 'success';
      saveMessage = result.restart_required
        ? 'Saved. Restart the backend for every change to take effect.'
        : 'Agent settings saved and applied.';
      await refresh();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the agent settings' });
    }
  }
</script>

<div class="sf-field">
  <span class="sf-label">Context management</span>
  <div class="strategy-list" role="radiogroup" aria-label="Context management strategy">
    {#each STRATEGY_OPTIONS as option (option.value)}
      <button
        type="button"
        role="radio"
        aria-checked={strategy === option.value}
        class="strategy-option"
        class:selected={strategy === option.value}
        onclick={() => (strategy = option.value)}
      >
        <span class="strategy-dot" aria-hidden="true"></span>
        <span class="strategy-text">
          <strong>{option.label}</strong>
          <small>{option.hint}</small>
        </span>
      </button>
    {/each}
  </div>

  {#if strategy === 'compact_tokens'}
    <label class="sf-sub trigger">
      <span>Compact at (tokens)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={thresholdTokens} placeholder="200000" />
    </label>
  {:else if strategy === 'compact_percent'}
    <label class="sf-sub trigger">
      <span>Compact at (% of context)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={thresholdPercent} placeholder="80" />
    </label>
  {:else if strategy === 'sliding_window'}
    <label class="sf-sub trigger">
      <span>Window cycles</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={windowCycles} placeholder="5" />
    </label>
  {/if}
</div>

<div class="sf-field">
  <span class="sf-label">Limits</span>
  <div class="sf-grid">
    <label class="sf-sub">
      <span>Memory entries</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={memoryMaxEntries} placeholder="100" />
    </label>
    <label class="sf-sub">
      <span>Memory budget (chars)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={memoryCharLimit} placeholder="8000" />
    </label>
    <label class="sf-sub">
      <span>Memory entry cap (chars)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={memoryValueMaxChars} placeholder="1000" />
    </label>
    <label class="sf-sub">
      <span>Tool output cap (chars)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={toolOutputMaxChars} placeholder="100000" />
    </label>
    <label class="sf-sub">
      <span>Tool timeout (seconds)</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={toolTimeout} placeholder="300" />
    </label>
  </div>
  <p class="sf-hint">
    How much the agent remembers about you, how large a single tool result may grow, and how
    long one tool call may run (30 to 900 seconds).
  </p>
</div>

<div class="sf-field">
  <label class="sf-label" for="setup-timezone">Timezone</label>
  <div class="tz-row">
    <input
      id="setup-timezone"
      class="sf-input"
      type="text"
      bind:value={timezone}
      placeholder="UTC"
    />
    {#if detectedTimezone && detectedTimezone !== timezone}
      <Button variant="ghost" size="sm" onclick={() => (timezone = detectedTimezone)}>
        Use {detectedTimezone}
      </Button>
    {/if}
  </div>
  <p class="sf-hint">
    IANA name, for example Europe/London. Timestamps the agent sees and schedules use this.
  </p>
</div>

<div class="sf-actions">
  <Button
    variant="primary"
    onclick={handleSave}
    disabled={saveStatus === 'saving' || !settings}
    loading={saveStatus === 'saving'}
  >
    {saveStatus === 'saving' ? 'Saving' : 'Save agent settings'}
  </Button>
</div>

{#if saveMessage}
  <div class="sf-result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
    <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
    <span>{saveMessage}</span>
  </div>
{/if}

<style>
  .strategy-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .strategy-option {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm-plus);
    padding: var(--spacing-sm-plus) var(--spacing-md);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    text-align: left;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .strategy-option:hover {
    border-color: var(--border-default);
    color: var(--text-primary);
  }

  .strategy-option.selected {
    border-color: var(--accent-primary);
    background: var(--accent-tint-bg);
    color: var(--text-primary);
  }

  .strategy-dot {
    width: 14px;
    height: 14px;
    margin-top: 3px;
    border-radius: var(--radius-full);
    border: 2px solid var(--border-default);
    flex-shrink: 0;
    transition: all var(--transition-fast);
  }

  .strategy-option.selected .strategy-dot {
    border-color: var(--accent-primary);
    background: var(--accent-primary);
    box-shadow: inset 0 0 0 2.5px var(--bg-elevated);
  }

  .strategy-text {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
    min-width: 0;
  }

  .strategy-text strong {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .strategy-text small {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .trigger {
    margin-top: var(--spacing-sm-plus);
    max-width: 280px;
  }

  .tz-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .tz-row .sf-input {
    flex: 1;
  }
</style>
