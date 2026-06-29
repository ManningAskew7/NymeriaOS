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
    /** Tri-state: 'default' inherits the global setting, 'true'/'false' override it. */
    sequentialToolExecution: 'default' | 'true' | 'false';
  }

  let {
    instructions = $bindable(),
    systemPrompt = $bindable(),
    injectTodosInPrompt = $bindable(),
    showAutonomousPrompts = $bindable(),
    showPromptMetadata = $bindable(),
    sequentialToolExecution = $bindable(),
  }: Props = $props();
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Custom Instructions"
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
    description="Replaces the base system prompt entirely for this thread. Leave empty to use the default."
  >
    <textarea
      id="system-prompt-input"
      class="text-input mono"
      bind:value={systemPrompt}
      placeholder="You are a specialized assistant that…"
      maxlength={50000}
      rows={10}
    ></textarea>
    <span class="char-count">{systemPrompt.length} / 50000</span>
  </ThreadSettingsSection>

  <ThreadSettingsSection title="Prompt Visibility">
    <label class="toggle-row">
      <input type="checkbox" bind:checked={injectTodosInPrompt} />
      <span class="toggle-label">Inject tasks into system prompt</span>
    </label>
    <p class="field-hint indented">
      Include active tasks directly in the system prompt so the LLM can see and
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

  <ThreadSettingsSection title="Tool Execution">
    <label class="select-label" for="thread-sequential-tools">Sequential tool execution</label>
    <select id="thread-sequential-tools" class="select-input" bind:value={sequentialToolExecution}>
      <option value="default">Default (inherit global)</option>
      <option value="true">On (run one at a time)</option>
      <option value="false">Off (run concurrently)</option>
    </select>
    <p class="field-hint last">
      When on, this thread runs each turn's tool calls one at a time, in the order
      the model emitted them, instead of concurrently. Slower for independent
      calls, but avoids parallel-execution races. The run_tools_in_order tool can
      still order a single batch even when this is off.
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
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
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

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-md) 0;
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
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

  .select-label {
    display: block;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    margin-bottom: 6px;
  }
  .select-input {
    width: 100%;
    max-width: 320px;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-family: inherit;
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    margin-bottom: var(--spacing-sm);
    transition: border-color var(--transition-fast);
  }
  .select-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }
</style>
