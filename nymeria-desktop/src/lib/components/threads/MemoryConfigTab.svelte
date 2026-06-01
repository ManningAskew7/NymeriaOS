<script lang="ts">
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Memory tab: the thread's persistent notepad, its size cap (moved here from
   * the old Agent tab, next to the notepad it actually limits), and the
   * dreaming scheduler (background self-reflection).
   */
  interface Props {
    // Notepad
    notepad: string;
    notepadLoaded: boolean;
    notepadCharLimit: number;
    // Memory cap
    memoryCharLimit: string | number;
    globalMemoryLimit?: number | null;
    // Dreaming
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
    notepad = $bindable(),
    notepadLoaded,
    notepadCharLimit,
    memoryCharLimit = $bindable(),
    globalMemoryLimit = null,
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
    title="Notepad"
    icon="pin"
    description="The agent's persistent memory for this thread. It survives context compaction and is reloaded each session, and is the same notepad the agent reads and writes with its memory tools."
  >
    {#if notepadLoaded}
      <textarea
        id="notepad-input"
        class="text-input mono"
        bind:value={notepad}
        placeholder="Notes the agent should remember for this thread..."
        rows={12}
      ></textarea>
      <span class="char-count">
        {notepad.length}{#if notepadCharLimit} / {notepadCharLimit}{/if}
      </span>
    {:else}
      <p class="field-hint">Loading notepad...</p>
    {/if}

    <div class="sub-field">
      <label class="field-label" for="thread-memory-limit-input">Memory Character Limit</label>
      <input
        id="thread-memory-limit-input"
        class="field-input"
        type="number"
        min="1"
        max="2000000"
        step="500"
        bind:value={memoryCharLimit}
        placeholder="Inherit global"
      />
      <span class="field-hint">
        Caps the notepad for this thread. Leave blank to inherit the global limit{globalMemoryLimit ? ` (${globalMemoryLimit.toLocaleString()} chars)` : ''}.
      </span>
    </div>
  </ThreadSettingsSection>

  <ThreadSettingsSection
    title="Dreaming"
    icon="clock"
    description="Background self-reflection. Dream turns run in a shadow thread and write only through the dream tool policy."
  >
    <label class="toggle-row">
      <input type="checkbox" bind:checked={dreamEnabled} />
      <span class="toggle-label">Enable dreaming for this thread</span>
    </label>

    <div class="dream-grid">
      <div class="field-group">
        <label class="field-label" for="dream-min-interval">Min Interval Hours</label>
        <input id="dream-min-interval" class="field-input" type="number" min="1" max="168" step="1" bind:value={dreamMinIntervalHours} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-idle">Min Idle Minutes</label>
        <input id="dream-min-idle" class="field-input" type="number" min="5" max="10080" step="5" bind:value={dreamMinIdleMinutes} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-min-turns">Min Turns Since Last</label>
        <input id="dream-min-turns" class="field-input" type="number" min="1" max="10000" step="1" bind:value={dreamMinTurnsSinceLast} />
      </div>
      <div class="field-group">
        <label class="field-label" for="dream-model">Dream Model</label>
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

  .text-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-family: inherit;
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    resize: vertical;
    transition: border-color var(--transition-fast);
  }
  .text-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .text-input::placeholder { color: var(--text-muted); }
  .text-input.mono {
    font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
  }

  .char-count {
    display: block;
    text-align: right;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: 6px;
  }

  .sub-field {
    margin-top: var(--spacing-md);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

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
    line-height: 1.5;
    margin: var(--spacing-xs) 0 0;
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

  .dream-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--spacing-md);
  }
  .dream-grid .field-group { margin-bottom: 0; }

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
