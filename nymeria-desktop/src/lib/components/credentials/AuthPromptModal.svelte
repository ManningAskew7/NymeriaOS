<script lang="ts">
  import { api } from '$lib/services/api';
  import type { AuthPromptEvent, AuthPromptField } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';
  import Modal from '../common/Modal.svelte';

  interface Props {
    prompt: AuthPromptEvent | null;
    onResolved: () => void;
  }

  let { prompt, onResolved }: Props = $props();

  let values = $state<Record<string, string>>({});
  let labelDraft = $state('');
  let submitting = $state(false);
  let lastError = $state<string | null>(null);
  let attempts = $state(0);
  let isOpen = $derived(prompt !== null);

  // Reset state every time a new prompt comes in.
  $effect(() => {
    if (prompt) {
      const initial: Record<string, string> = {};
      for (const field of prompt.fields) {
        initial[field.name] = '';
      }
      values = initial;
      labelDraft = prompt.account_label || '';
      submitting = false;
      lastError = null;
      attempts = 0;
    }
  });

  function close() {
    onResolved();
  }

  async function handleClose() {
    if (!prompt) {
      close();
      return;
    }
    if (submitting) return;
    // X / Esc / backdrop — return last error to the agent so it can help.
    await api.exitCredentialPrompt(prompt.prompt_id, lastError, attempts);
    close();
  }

  async function handleCancel() {
    if (!prompt || submitting) return;
    await api.cancelCredentialPrompt(prompt.prompt_id);
    close();
  }

  async function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    if (!prompt || submitting) return;
    submitting = true;
    try {
      const trimmedLabel = labelDraft.trim();
      const result = await api.submitCredentialPrompt(prompt.prompt_id, {
        secret_fields: values,
        account_label: trimmedLabel || null,
      });
      attempts = result.attempts;
      if (result.ok) {
        close();
        return;
      }
      lastError = result.error || 'Connection test failed';
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    } finally {
      submitting = false;
    }
  }

  function fieldType(field: AuthPromptField): 'password' | 'text' | 'textarea' {
    if (field.kind === 'textarea') return 'textarea';
    return field.secret ? 'password' : 'text';
  }
</script>

<Modal title={prompt ? `Connect ${prompt.display_name}` : ''} {isOpen} onClose={handleClose}>
  {#if prompt}
    {@const showLabelEditor = prompt.existing_accounts.length > 0 || prompt.account_label}
    <form onsubmit={handleSubmit} class="auth-form">
      {#if prompt.existing_accounts.length > 0}
        <div class="existing-banner">
          <Icon name="info" size={14} />
          <span>
            Already connected:
            {prompt.existing_accounts
              .map((acc) => acc.account_label || acc.name)
              .join(', ')}.
            Add another?
          </span>
        </div>
      {/if}

      {#each prompt.fields as field (field.name)}
        <div class="field">
          <label for={`auth-field-${prompt.prompt_id}-${field.name}`}>
            {field.label}
          </label>
          {#if fieldType(field) === 'textarea'}
            <textarea
              id={`auth-field-${prompt.prompt_id}-${field.name}`}
              bind:value={values[field.name]}
              placeholder={field.placeholder || ''}
              rows="4"
              autocomplete="off"
              spellcheck="false"
              required
            ></textarea>
          {:else}
            <input
              id={`auth-field-${prompt.prompt_id}-${field.name}`}
              type={fieldType(field)}
              bind:value={values[field.name]}
              placeholder={field.placeholder || ''}
              autocomplete="off"
              spellcheck="false"
              required
            />
          {/if}
          {#if field.help}
            <small class="help">{field.help}</small>
          {/if}
        </div>
      {/each}

      {#if showLabelEditor}
        <div class="field">
          <label for={`auth-label-${prompt.prompt_id}`}>Name this connection</label>
          <input
            id={`auth-label-${prompt.prompt_id}`}
            type="text"
            bind:value={labelDraft}
            placeholder="e.g. work, personal"
            autocomplete="off"
          />
        </div>
      {/if}

      {#if lastError}
        <div class="error">
          <Icon name="alert-triangle" size={14} />
          <span>{lastError}</span>
        </div>
      {/if}

      <div class="actions">
        <Button variant="ghost" type="button" onclick={handleCancel} disabled={submitting}>
          Cancel
        </Button>
        <Button type="submit" disabled={submitting}>
          {#if submitting}
            Testing…
          {:else if attempts > 0}
            Retry
          {:else}
            Connect
          {/if}
        </Button>
      </div>
    </form>
  {/if}
</Modal>

<style>
  .auth-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(440px, 80vw);
  }

  .existing-banner {
    display: flex;
    gap: var(--spacing-xs);
    align-items: flex-start;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: var(--bg-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .field label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .field input,
  .field textarea {
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-input, var(--glass-bg));
    color: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
  }

  .field textarea {
    resize: vertical;
    min-height: 80px;
  }

  .field input:focus,
  .field textarea:focus {
    outline: none;
    border-color: var(--accent);
  }

  .help {
    color: var(--text-tertiary, var(--text-secondary));
    font-size: var(--font-size-xs);
  }

  .error {
    display: flex;
    gap: var(--spacing-xs);
    align-items: flex-start;
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--danger, #c0392b) 12%, transparent);
    color: var(--danger, #c0392b);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }
</style>
