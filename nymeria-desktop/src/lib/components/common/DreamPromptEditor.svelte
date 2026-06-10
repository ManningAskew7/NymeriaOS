<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import type { DreamPromptInfo } from '$lib/types';
  import Button from './Button.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  /**
   * Global (admin) editor for the two dream prompts: the dream system prompt and
   * the dream kickoff message. These are the defaults a per-thread Dreaming tab
   * override layers on top of. Edits save to a data-dir override (the shipped
   * package files are never touched) and take effect on the next dream cycle, so
   * no agent reload is needed.
   */

  type FieldKey = 'system' | 'kickoff';

  interface FieldState {
    content: string;
    original: string;
    defaultContent: string;
    isOverride: boolean;
    saving: boolean;
    status: 'idle' | 'success' | 'error';
    message: string;
  }

  const MAX_LEN: Record<FieldKey, number> = { system: 50000, kickoff: 10000 };

  function blank(): FieldState {
    return {
      content: '',
      original: '',
      defaultContent: '',
      isOverride: false,
      saving: false,
      status: 'idle',
      message: ''
    };
  }

  let loading = $state(true);
  let loadError = $state('');
  const fields = $state<Record<FieldKey, FieldState>>({
    system: blank(),
    kickoff: blank()
  });

  function applyInfo(key: FieldKey, info: DreamPromptInfo) {
    const f = fields[key];
    f.content = info.content;
    f.original = info.content;
    f.defaultContent = info.defaultContent;
    f.isOverride = info.isOverride;
  }

  onMount(load);

  async function load() {
    loading = true;
    loadError = '';
    try {
      const info = await api.getDreamPrompts();
      applyInfo('system', info.system);
      applyInfo('kickoff', info.kickoff);
    } catch (e) {
      loadError = humanizeErrorText(e, { action: 'load', resource: 'the dream prompts' });
    } finally {
      loading = false;
    }
  }

  function payloadFor(key: FieldKey, value: string): { system?: string; kickoff?: string } {
    return key === 'system' ? { system: value } : { kickoff: value };
  }

  async function save(key: FieldKey) {
    const f = fields[key];
    f.saving = true;
    f.status = 'idle';
    f.message = '';
    try {
      const info = await api.updateDreamPrompts(payloadFor(key, f.content));
      applyInfo(key, info[key]);
      f.status = 'success';
      f.message = info[key].isOverride
        ? 'Saved. New dreams use this now.'
        : 'Override cleared. Using the shipped default.';
    } catch (e) {
      f.status = 'error';
      f.message = humanizeErrorText(e, { action: 'save', resource: 'the dream prompt' });
    } finally {
      f.saving = false;
    }
  }

  async function resetToDefault(key: FieldKey) {
    const f = fields[key];
    f.saving = true;
    f.status = 'idle';
    f.message = '';
    try {
      const info = await api.updateDreamPrompts(payloadFor(key, ''));
      applyInfo(key, info[key]);
      f.status = 'success';
      f.message = 'Reset to the shipped default.';
    } catch (e) {
      f.status = 'error';
      f.message = humanizeErrorText(e, { action: 'reset', resource: 'the dream prompt' });
    } finally {
      f.saving = false;
    }
  }
</script>

<div class="dream-editor">
  <header class="editor-header">
    <div class="editor-heading">
      <h3 class="section-heading">Dream Prompts</h3>
      <p class="field-hint">
        The global defaults for the background dream cycle. The system prompt is the
        dream's role, phases, and rules; the kickoff message is the first message sent
        to each dreaming thread. A thread's own Dreaming tab can override either of
        these. Edits save to a data-dir override and apply on the next dream; the
        shipped default files are never modified.
      </p>
    </div>
  </header>

  {#if loading}
    <p class="loading">Loading dream prompts…</p>
  {:else if loadError}
    <p class="status error">{loadError}</p>
  {:else}
    {@render promptBlock('system', 'Dream system prompt')}
    {@render promptBlock('kickoff', 'Dream kickoff message')}
  {/if}
</div>

{#snippet promptBlock(key: FieldKey, title: string)}
  {@const f = fields[key]}
  <section class="prompt-section">
    <div class="prompt-head">
      <h4 class="prompt-title">{title}</h4>
      <span class="badge" class:override={f.isOverride}>
        {f.isOverride ? 'Custom override' : 'Default'}
      </span>
    </div>

    {#if key === 'kickoff'}
      <p class="field-hint">
        Placeholders <code>{'{parent_thread_id}'}</code> and
        <code>{'{parent_instructions}'}</code> are substituted at dream time.
      </p>
    {/if}

    <textarea
      class="prompt-input"
      bind:value={f.content}
      maxlength={MAX_LEN[key]}
      rows={key === 'system' ? 16 : 8}
      spellcheck="false"
    ></textarea>
    <div class="editor-foot">
      <span class="char-count">{f.content.length.toLocaleString()} / {MAX_LEN[key].toLocaleString()}</span>
      {#if f.status !== 'idle'}
        <span class="status" class:error={f.status === 'error'} class:success={f.status === 'success'}>
          {f.message}
        </span>
      {/if}
    </div>
    <div class="actions">
      <Button
        variant="primary"
        onclick={() => save(key)}
        disabled={f.saving || f.content === f.original}
        loading={f.saving}
      >
        Save
      </Button>
      <Button
        variant="secondary"
        onclick={() => resetToDefault(key)}
        disabled={f.saving || (!f.isOverride && f.content === f.defaultContent)}
      >
        Reset to default
      </Button>
    </div>
  </section>
{/snippet}

<style>
  .dream-editor {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    padding: var(--spacing-lg);
  }

  .editor-heading {
    min-width: 0;
  }

  .section-heading {
    margin: 0 0 6px 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-sm);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .field-hint code {
    font-family: var(--font-mono);
    color: var(--text-secondary, var(--text-primary));
  }

  .prompt-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .prompt-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .prompt-title {
    margin: 0;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
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
    min-height: 140px;
    font-family: var(--font-mono);
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
    transition: border-color var(--transition-fast);
  }

  .prompt-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
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
