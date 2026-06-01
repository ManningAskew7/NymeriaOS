<script lang="ts">
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Dreaming tab: background self-reflection scheduler. Dream turns run in a
   * shadow thread and write only through the dream tool policy.
   */
  interface Props {
    dreamEnabled: boolean;
    dreamMinIntervalHours: string;
    dreamMinIdleMinutes: string;
    dreamMinTurnsSinceLast: string;
    dreamModel: string;
    lastDreamAt?: string | null;
    dreamRunning: boolean;
    dreamStatus: string;
    saving: boolean;
    hasUnsavedChanges: boolean;
    onRunDream: () => void;
  }

  let {
    dreamEnabled = $bindable(),
    dreamMinIntervalHours = $bindable(),
    dreamMinIdleMinutes = $bindable(),
    dreamMinTurnsSinceLast = $bindable(),
    dreamModel = $bindable(),
    lastDreamAt = null,
    dreamRunning,
    dreamStatus,
    saving,
    hasUnsavedChanges,
    onRunDream,
  }: Props = $props();

  const runDisabled = $derived(saving || dreamRunning || !dreamEnabled || hasUnsavedChanges);
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Dreaming"
    description="Background self-reflection for this thread. Dream turns run in a shadow thread and write only through the dream tool policy."
  >
    <label class="toggle-row">
      <input type="checkbox" bind:checked={dreamEnabled} />
      <span class="toggle-label">Enable dreaming for this thread</span>
    </label>

    <div class="grid-2">
      <div class="field-group">
        <label class="field-label" for="dream-min-interval">Min interval (hours)</label>
        <input id="dream-min-interval" class="field-input" type="number" min="1" max="168" step="1" bind:value={dreamMinIntervalHours} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-idle">Min idle (minutes)</label>
        <input id="dream-min-idle" class="field-input" type="number" min="5" max="10080" step="5" bind:value={dreamMinIdleMinutes} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-turns">Min turns since last</label>
        <input id="dream-min-turns" class="field-input" type="number" min="1" max="10000" step="1" bind:value={dreamMinTurnsSinceLast} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-model">Dream model</label>
        <input id="dream-model" class="field-input" type="text" bind:value={dreamModel} placeholder="Default model" maxlength={120} />
      </div>
    </div>

    {#if lastDreamAt}
      <p class="field-hint">Last dream: {new Date(lastDreamAt).toLocaleString()}</p>
    {/if}

    <div class="dream-actions">
      <button
        class="btn btn-primary"
        type="button"
        onclick={onRunDream}
        disabled={runDisabled}
        title={hasUnsavedChanges ? 'Save changes before running a dream' : 'Run dream now'}
      >
        {dreamRunning ? 'Starting...' : 'Run Dream'}
      </button>
      {#if dreamStatus}
        <span class="dream-status">{dreamStatus}</span>
      {/if}
    </div>
  </ThreadSettingsSection>
</div>

<style>
  .tab-body {
    padding: var(--spacing-lg);
  }

  .field-group { margin-bottom: 0; }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 4px;
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin: var(--spacing-md) 0 0;
  }

  .field-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }
  .field-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .field-input::placeholder { color: var(--text-muted); }

  .grid-2 {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--spacing-md);
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-md) 0;
    cursor: pointer;
  }
  .toggle-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    accent-color: var(--accent-primary);
    cursor: pointer;
  }
  .toggle-label {
    font-size: var(--font-size-sm);
    line-height: 1.4;
    color: var(--text-primary);
  }

  .dream-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-top: var(--spacing-md);
  }
  .dream-status {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }
  .btn-primary:hover:not(:disabled) { filter: brightness(1.1); }
</style>
