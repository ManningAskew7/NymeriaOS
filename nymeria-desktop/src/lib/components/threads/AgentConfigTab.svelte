<script lang="ts">
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  /**
   * Agent tab: expose this thread as a callable sub-agent that the main agent
   * (or other threads) can delegate tasks to.
   */
  interface Props {
    isCallable: boolean;
    callableName: string;
    callableDescription: string;
  }

  let {
    isCallable = $bindable(),
    callableName = $bindable(),
    callableDescription = $bindable(),
  }: Props = $props();
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Publish as Tool"
    description="Mark this thread as a callable sub-agent so Nymeria can delegate tasks to it."
  >
    <label class="toggle-row">
      <input type="checkbox" bind:checked={isCallable} />
      <span class="toggle-label">Make this thread callable</span>
    </label>

    {#if isCallable}
      <div class="field-group">
        <label class="field-label" for="callable-name-input">Tool name</label>
        <input
          id="callable-name-input"
          class="field-input"
          type="text"
          bind:value={callableName}
          placeholder="e.g. ResearchAgent"
          maxlength={64}
        />
        <span class="field-hint">The name Nymeria uses to call this thread.</span>
      </div>

      <div class="field-group last">
        <label class="field-label" for="callable-desc-input">Tool description</label>
        <textarea
          id="callable-desc-input"
          class="text-input"
          bind:value={callableDescription}
          placeholder="e.g. Autonomous web research that finds information, summarizes articles, and compiles reports"
          maxlength={500}
          rows={3}
        ></textarea>
        <span class="char-count">{callableDescription.length} / 500</span>
        <span class="field-hint">What the model sees as the tool description. Describe when to use this thread.</span>
      </div>
    {/if}
  </ThreadSettingsSection>
</div>

<style>
  .tab-body {
    padding: var(--spacing-lg);
  }

  .field-group { margin-bottom: var(--spacing-md); }
  .field-group.last { margin-bottom: 0; }

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
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin-top: var(--spacing-xs);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .field-input,
  .text-input {
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
  .text-input { font-family: inherit; resize: vertical; }
  .field-input:focus,
  .text-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .field-input::placeholder,
  .text-input::placeholder { color: var(--text-muted); }

  .char-count {
    display: block;
    text-align: right;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: 6px;
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
</style>
