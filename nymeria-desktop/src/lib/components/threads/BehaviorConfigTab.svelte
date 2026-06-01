<script lang="ts">
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Behavior tab: everything that shapes what the model is told. Custom
   * instructions (appended), a full system-prompt override (replaces), and the
   * prompt-visibility toggles that used to be buried under Instructions >
   * Advanced.
   */
  interface Props {
    instructions: string;
    systemPrompt: string;
    injectTodosInPrompt: boolean;
    showAutonomousPrompts: boolean;
    showPromptMetadata: boolean;
  }

  let {
    instructions = $bindable(),
    systemPrompt = $bindable(),
    injectTodosInPrompt = $bindable(),
    showAutonomousPrompts = $bindable(),
    showPromptMetadata = $bindable(),
  }: Props = $props();
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Custom Instructions"
    icon="fileText"
    description="Appended to the base system prompt for this thread only."
  >
    <textarea
      id="thread-instructions"
      class="text-input"
      bind:value={instructions}
      placeholder="e.g. Focus on email management. Be concise. Always check the calendar before scheduling."
      maxlength={5000}
      rows={7}
    ></textarea>
    <span class="char-count">{instructions.length} / 5000</span>
  </ThreadSettingsSection>

  <ThreadSettingsSection
    title="System Prompt Override"
    icon="textEdit"
    description="Replaces the base system prompt entirely for this thread. Leave empty to use the default."
  >
    <textarea
      id="system-prompt-input"
      class="text-input mono"
      bind:value={systemPrompt}
      placeholder="You are a specialized assistant that..."
      maxlength={50000}
      rows={10}
    ></textarea>
    <span class="char-count">{systemPrompt.length} / 50000</span>
  </ThreadSettingsSection>

  <ThreadSettingsSection title="Prompt Visibility" icon="settings">
    <label class="toggle-row">
      <input type="checkbox" bind:checked={injectTodosInPrompt} />
      <span class="toggle-label">Inject TODOs into system prompt</span>
    </label>
    <p class="field-hint indented">
      Include active TODOs directly in the system prompt so the LLM can see and
      act on them without tool calls. Uses extra context tokens.
    </p>

    <label class="toggle-row">
      <input type="checkbox" bind:checked={showAutonomousPrompts} />
      <span class="toggle-label">Force show autonomous prompts on this thread</span>
    </label>
    <p class="field-hint indented">
      Per-thread override. When the global "Show autonomous prompts" setting
      (Settings, Appearance) is off, enable this to still show scheduler,
      watchdog, and trigger prompts on this specific thread.
    </p>

    <label class="toggle-row">
      <input type="checkbox" bind:checked={showPromptMetadata} />
      <span class="toggle-label">Show prompt metadata</span>
    </label>
    <p class="field-hint indented last">
      Show the time context and trigger type prepended to each message. Useful
      for debugging prompt flow and callable thread routing.
    </p>
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

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-md) 0;
  }
  .field-hint.indented { margin-left: 28px; }
  .field-hint.last { margin-bottom: 0; }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 6px 0;
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
</style>
