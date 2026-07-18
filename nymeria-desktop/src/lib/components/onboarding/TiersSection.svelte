<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type { ServerSettings, ServerSettingsUpdate } from '$lib/types';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    settings: ServerSettings | null;
    refresh: () => Promise<void>;
  }

  let { settings, refresh }: Props = $props();

  // Mirrors the CLI llm_tuning step's EFFORT_CHOICES; '' = provider default.
  const EFFORT_OPTIONS = [
    { value: '', label: 'Default', hint: 'Let the provider decide' },
    { value: 'off', label: 'Off', hint: 'No extended thinking' },
    { value: 'low', label: 'Low', hint: 'Quick answers' },
    { value: 'medium', label: 'Medium', hint: 'Recommended balance' },
    { value: 'high', label: 'High', hint: 'Harder problems' },
    { value: 'xhigh', label: 'XHigh', hint: 'Deep reasoning' },
    { value: 'max', label: 'Max', hint: 'Everything the model has' },
  ];

  let effort = $state('');
  let temperature = $state('');
  let maxTokens = $state('');
  let topP = $state('');
  let topK = $state('');
  let fastModel = $state('');
  let smartModel = $state('');
  let backgroundModel = $state('');
  let fallbackModels = $state('');
  let fallbackHoldSeconds = $state('');

  let saveStatus = $state<'idle' | 'saving' | 'success' | 'error'>('idle');
  let saveMessage = $state('');
  let initializedFor = $state<ServerSettings | null>(null);

  $effect(() => {
    if (!settings || settings === initializedFor) return;
    initializedFor = settings;
    effort = settings.llm_reasoning_effort ?? '';
    temperature = settings.llm_temperature != null ? String(settings.llm_temperature) : '';
    maxTokens = settings.llm_max_tokens != null ? String(settings.llm_max_tokens) : '';
    topP = settings.llm_top_p != null ? String(settings.llm_top_p) : '';
    topK = settings.llm_top_k != null ? String(settings.llm_top_k) : '';
    fastModel = settings.llm_fast_model ?? '';
    smartModel = settings.llm_smart_model ?? '';
    backgroundModel = settings.llm_background_model ?? '';
    fallbackModels = (settings.llm_fallback_models ?? []).join(', ');
    fallbackHoldSeconds =
      settings.llm_fallback_hold_seconds != null ? String(settings.llm_fallback_hold_seconds) : '';
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
      const updates: ServerSettingsUpdate = {};
      // Effort: only when changed; '' clears back to the provider default
      // (explicit null rides the clearable-null PATCH path).
      if (effort !== (settings?.llm_reasoning_effort ?? '')) {
        updates.llm_reasoning_effort = effort ? effort : null;
      }
      const temp = numberOrUndefined(temperature);
      if (temp !== undefined) updates.llm_temperature = temp;
      const mt = numberOrUndefined(maxTokens);
      if (mt !== undefined) updates.llm_max_tokens = mt;
      const tp = numberOrUndefined(topP);
      if (tp !== undefined) updates.llm_top_p = tp;
      const tk = numberOrUndefined(topK);
      if (tk !== undefined) updates.llm_top_k = tk;
      if (fastModel.trim() !== (settings?.llm_fast_model ?? '')) {
        updates.llm_fast_model = fastModel.trim();
      }
      if (smartModel.trim() !== (settings?.llm_smart_model ?? '')) {
        updates.llm_smart_model = smartModel.trim();
      }
      if (backgroundModel.trim() !== (settings?.llm_background_model ?? '')) {
        updates.llm_background_model = backgroundModel.trim();
      }
      const fallbacks = fallbackModels
        .split(',')
        .map((m) => m.trim())
        .filter(Boolean)
        .join(',');
      if (fallbacks !== (settings?.llm_fallback_models ?? []).join(',')) {
        updates.llm_fallback_models = fallbacks;
      }
      const hold = numberOrUndefined(fallbackHoldSeconds);
      if (hold !== undefined) updates.llm_fallback_hold_seconds = hold;

      const result = await api.updateServerSettings(updates);
      saveStatus = 'success';
      saveMessage = result.restart_required
        ? 'Saved. Restart the backend for every change to take effect.'
        : 'Model settings saved and applied.';
      await refresh();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the model settings' });
    }
  }
</script>

<div class="sf-field">
  <span class="sf-label">Reasoning effort</span>
  <div class="effort-row" role="radiogroup" aria-label="Reasoning effort">
    {#each EFFORT_OPTIONS as option (option.value)}
      <button
        type="button"
        role="radio"
        aria-checked={effort === option.value}
        class="effort-chip"
        class:selected={effort === option.value}
        title={option.hint}
        onclick={() => (effort = option.value)}
      >
        {option.label}
      </button>
    {/each}
  </div>
  <p class="sf-hint">
    How much thinking the model does before answering. Medium is the recommended balance;
    levels above a model's ceiling are clamped automatically.
  </p>
</div>

<div class="sf-field">
  <span class="sf-label">Sampling</span>
  <div class="sf-grid">
    <label class="sf-sub">
      <span>Temperature</span>
      <input class="sf-input" type="text" inputmode="decimal" bind:value={temperature} placeholder="1.0" />
    </label>
    <label class="sf-sub">
      <span>Max output tokens</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={maxTokens} placeholder="Model default" />
    </label>
    <label class="sf-sub">
      <span>Top-p</span>
      <input class="sf-input" type="text" inputmode="decimal" bind:value={topP} placeholder="Unset" />
    </label>
    <label class="sf-sub">
      <span>Top-k</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={topK} placeholder="Unset" />
    </label>
  </div>
  <p class="sf-hint">Blank fields keep the current or model-default value.</p>
</div>

<div class="sf-field">
  <span class="sf-label">Model tiers</span>
  <div class="sf-grid">
    <label class="sf-sub">
      <span>Fast</span>
      <input
        class="sf-input"
        type="text"
        bind:value={fastModel}
        placeholder={settings?.llm_fast_model_resolved ?? 'Derived from main model'}
      />
    </label>
    <label class="sf-sub">
      <span>Smart</span>
      <input
        class="sf-input"
        type="text"
        bind:value={smartModel}
        placeholder={settings?.llm_smart_model_resolved ?? 'Derived from main model'}
      />
    </label>
    <label class="sf-sub">
      <span>Background</span>
      <input
        class="sf-input"
        type="text"
        bind:value={backgroundModel}
        placeholder={settings?.llm_background_model_resolved ?? 'Derived from main model'}
      />
    </label>
  </div>
  <p class="sf-hint">
    Cheap, capable, and off-hours aliases other features resolve against. Accepts
    <code>provider:model</code> refs; blank derives from the main model.
  </p>
</div>

<div class="sf-field">
  <span class="sf-label">Fallbacks</span>
  <div class="sf-grid two">
    <label class="sf-sub">
      <span>Fallback models</span>
      <input class="sf-input" type="text" bind:value={fallbackModels} placeholder="model-a, model-b" />
    </label>
    <label class="sf-sub">
      <span>Hold seconds</span>
      <input class="sf-input" type="text" inputmode="numeric" bind:value={fallbackHoldSeconds} placeholder="7200" />
    </label>
  </div>
  <p class="sf-hint">
    Tried in order when the primary model errors; the hold keeps traffic on the fallback
    before retrying the primary.
  </p>
</div>

<div class="sf-actions">
  <Button
    variant="primary"
    onclick={handleSave}
    disabled={saveStatus === 'saving' || !settings}
    loading={saveStatus === 'saving'}
  >
    {saveStatus === 'saving' ? 'Saving' : 'Save model settings'}
  </Button>
</div>

{#if saveMessage}
  <div class="sf-result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
    <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
    <span>{saveMessage}</span>
  </div>
{/if}

<style>
  .effort-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-sm);
  }

  .effort-chip {
    padding: var(--spacing-xs) var(--spacing-sm-plus);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .effort-chip:hover {
    border-color: var(--border-default);
    color: var(--text-primary);
  }

  .effort-chip.selected {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    color: var(--text-on-accent);
  }
</style>
