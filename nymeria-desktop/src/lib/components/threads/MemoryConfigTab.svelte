<script lang="ts">
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Memory tab: the thread's persistent notepad and its size cap (moved here
   * from the old Agent tab, next to the notepad it actually limits).
   */
  interface Props {
    notepad: string;
    notepadLoaded: boolean;
    notepadCharLimit: number;
    memoryCharLimit: string | number;
    globalMemoryLimit?: number | null;
  }

  let {
    notepad = $bindable(),
    notepadLoaded,
    notepadCharLimit,
    memoryCharLimit = $bindable(),
    globalMemoryLimit = null,
  }: Props = $props();
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Notepad"
    description="The agent's persistent memory for this thread. It survives context compaction and is reloaded each session, and is the same notepad the agent reads and writes with its memory tools."
  >
    {#if notepadLoaded}
      <textarea
        id="notepad-input"
        class="text-input mono"
        bind:value={notepad}
        placeholder="Notes the agent should remember for this thread..."
        rows={14}
      ></textarea>
      <span class="char-count">
        {notepad.length}{#if notepadCharLimit} / {notepadCharLimit}{/if}
      </span>
    {:else}
      <p class="field-hint">Loading notepad...</p>
    {/if}

    <div class="sub-field">
      <label class="field-label" for="thread-memory-limit-input">Memory character limit</label>
      <input
        id="thread-memory-limit-input"
        class="field-input narrow"
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
    font-family: var(--font-mono);
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
    /* §4 — labels recede behind the input value (which is --text-primary),
       so the eye finds the answer before the question. */
    color: var(--text-secondary);
    margin-bottom: 4px;
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin: var(--spacing-xs) 0 0;
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
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
  .field-input.narrow { max-width: 280px; }
  .field-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .field-input::placeholder { color: var(--text-muted); }
</style>
