<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import type { SystemPromptInfo } from '$lib/types';
  import Button from './Button.svelte';

  const MAX_LEN = 100000;

  let content = $state('');
  let defaultContent = $state('');
  let isOverride = $state(false);
  let original = $state('');
  let loading = $state(true);
  let saving = $state(false);
  let status = $state<'idle' | 'success' | 'error'>('idle');
  let message = $state('');

  let dirty = $derived(content !== original);
  let canReset = $derived(isOverride || content !== defaultContent);

  onMount(load);

  function apply(info: SystemPromptInfo) {
    content = info.content;
    defaultContent = info.defaultContent;
    isOverride = info.isOverride;
    original = info.content;
  }

  async function load() {
    loading = true;
    status = 'idle';
    message = '';
    try {
      apply(await api.getSystemPrompt());
    } catch (e) {
      status = 'error';
      message = e instanceof Error ? e.message : 'Failed to load system prompt';
    } finally {
      loading = false;
    }
  }

  async function save() {
    saving = true;
    status = 'idle';
    message = '';
    try {
      const info = await api.updateSystemPrompt(content);
      apply(info);
      status = 'success';
      message = info.isOverride
        ? 'Saved. New chats use this system prompt now.'
        : 'Override cleared. Using the shipped default.';
    } catch (e) {
      status = 'error';
      message = e instanceof Error ? e.message : 'Failed to save system prompt';
    } finally {
      saving = false;
    }
  }

  async function resetToDefault() {
    saving = true;
    status = 'idle';
    message = '';
    try {
      apply(await api.resetSystemPrompt());
      status = 'success';
      message = 'Reset to the shipped default system prompt.';
    } catch (e) {
      status = 'error';
      message = e instanceof Error ? e.message : 'Failed to reset system prompt';
    } finally {
      saving = false;
    }
  }
</script>

<div class="prompt-editor">
  <header class="editor-header">
    <div class="editor-heading">
      <h3 class="section-title">System Prompt</h3>
      <p class="field-hint">
        The base persona and instructions applied to every thread (soul.md).
        Per-thread custom instructions and overrides still layer on top of this.
        Edits save to a data-dir override and take effect for new chats right
        away; the shipped default file is never modified.
      </p>
    </div>
    <span class="badge" class:override={isOverride}>
      {isOverride ? 'Custom override' : 'Default'}
    </span>
  </header>

  {#if loading}
    <p class="loading">Loading system prompt...</p>
  {:else}
    <textarea
      class="prompt-input"
      bind:value={content}
      maxlength={MAX_LEN}
      rows={20}
      placeholder="You are Nymeria, a helpful AI assistant..."
      spellcheck="false"
    ></textarea>
    <div class="editor-foot">
      <span class="char-count">{content.length.toLocaleString()} / {MAX_LEN.toLocaleString()}</span>
      {#if status !== 'idle'}
        <span class="status" class:error={status === 'error'} class:success={status === 'success'}>
          {message}
        </span>
      {/if}
    </div>
    <div class="actions">
      <Button variant="primary" onclick={save} disabled={saving || !dirty} loading={saving}>
        Save
      </Button>
      <Button variant="secondary" onclick={resetToDefault} disabled={saving || !canReset}>
        Reset to default
      </Button>
    </div>
  {/if}
</div>

<style>
  .prompt-editor {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
  }

  .editor-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .editor-heading {
    min-width: 0;
  }

  .section-title {
    margin: 0 0 6px 0;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0;
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .badge {
    flex-shrink: 0;
    padding: 2px 10px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
  }

  .badge.override {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .prompt-input {
    width: 100%;
    padding: var(--spacing-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    resize: vertical;
    min-height: 280px;
    font-family: var(--font-mono);
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
    transition: border-color var(--transition-fast);
  }

  .prompt-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
  }

  .prompt-input::placeholder {
    color: var(--text-muted);
  }

  .editor-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .char-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .status {
    font-size: var(--font-size-xs);
    text-align: right;
  }

  .status.success {
    color: var(--success);
  }

  .status.error {
    color: var(--error);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
  }

  .loading {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }
</style>
