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
    dreamSystemPrompt: string;
    dreamKickoffPrompt: string;
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
    dreamSystemPrompt = $bindable(),
    dreamKickoffPrompt = $bindable(),
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
    description="Lets this thread reflect on its own. When the conversation goes quiet, Nymeria wakes in a private shadow copy of this thread, re-reads the recent conversation and its memory, consolidates or prunes notes, and can schedule follow-up tasks. A dream never replies here and never interrupts you; it only updates memory and todos in the background."
  >
    <label class="toggle-row">
      <input type="checkbox" bind:checked={dreamEnabled} />
      <span class="toggle-label">Enable dreaming for this thread</span>
    </label>
    <p class="section-note">
      While enabled, a dream starts on its own once all three thresholds below are met. Nymeria checks roughly every 10 minutes, so a dream may begin a few minutes after the thread falls idle.
    </p>

    <div class="grid-2">
      <div class="field-group">
        <label class="field-label" for="dream-min-interval">Min interval (hours)</label>
        <input id="dream-min-interval" class="field-input" type="number" min="1" max="168" step="1" bind:value={dreamMinIntervalHours} />
        <span class="input-hint">Shortest gap between dreams. The clock starts at the last dream, so this caps how often a thread can dream (default 6).</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-idle">Min idle (minutes)</label>
        <input id="dream-min-idle" class="field-input" type="number" min="5" max="10080" step="5" bind:value={dreamMinIdleMinutes} />
        <span class="input-hint">How long the thread must be quiet before a dream may start, so a live conversation is never disturbed. Minimum 5 (default 30).</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-turns">Min turns since last</label>
        <input id="dream-min-turns" class="field-input" type="number" min="1" max="10000" step="1" bind:value={dreamMinTurnsSinceLast} />
        <span class="input-hint">How many of your messages must build up since the last dream, so there is enough new material to reflect on (default 10).</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-model">Dream model</label>
        <input id="dream-model" class="field-input" type="text" bind:value={dreamModel} placeholder="Default model" maxlength={120} />
        <span class="input-hint">Model the dream turn runs on. Leave blank to use your global default model.</span>
      </div>
    </div>

    <div class="prompt-group">
      <label class="field-label" for="dream-system-prompt">Dream system prompt (override)</label>
      <textarea
        id="dream-system-prompt"
        class="prompt-textarea"
        bind:value={dreamSystemPrompt}
        rows="6"
        maxlength={50000}
        placeholder="Leave blank to use the global default (Settings, Dream tab)"
        spellcheck="false"
      ></textarea>
      <span class="input-hint">Replaces the dream's whole system prompt (its role, cycle phases, and rules) for this thread only. Leave blank to use the global default.</span>
    </div>

    <div class="prompt-group">
      <label class="field-label" for="dream-kickoff-prompt">Dream kickoff message (override)</label>
      <textarea
        id="dream-kickoff-prompt"
        class="prompt-textarea"
        bind:value={dreamKickoffPrompt}
        rows="6"
        maxlength={10000}
        placeholder="Leave blank to use the global default (Settings, Dream tab)"
        spellcheck="false"
      ></textarea>
      <span class="input-hint">
        The first message sent to the dreaming thread. Placeholders <code>{'{parent_thread_id}'}</code> and <code>{'{parent_instructions}'}</code> are filled in at dream time. Leave blank to use the global default.
      </span>
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
    <p class="input-hint">
      Run Dream triggers one dream right now and ignores the thresholds above (it still needs dreaming enabled and your changes saved first). Use it to preview what a dream does for this thread.
    </p>
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

  /* Per-field help under an input. */
  .input-hint {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin-top: var(--spacing-xs);
  }

  /* Explanatory paragraph under the enable toggle. */
  .section-note {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-md);
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

  .prompt-group {
    margin-top: var(--spacing-md);
  }

  .prompt-textarea {
    width: 100%;
    padding: var(--spacing-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    resize: vertical;
    min-height: 96px;
    font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
    transition: border-color var(--transition-fast);
  }
  .prompt-textarea:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .prompt-textarea::placeholder { color: var(--text-muted); }

  .input-hint code {
    font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: calc(var(--font-size-xs) - 0.5px);
    color: var(--text-secondary, var(--text-primary));
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
