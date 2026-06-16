<script lang="ts">
  import type { MCPServerCreateRequest } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    mode: 'add' | 'edit';
    initialData?: Partial<MCPServerCreateRequest>;
    loading: boolean;
    error: string | null;
    onSubmit: (data: MCPServerCreateRequest) => void;
    onCancel: () => void;
  }

  let { mode, initialData, loading, error, onSubmit, onCancel }: Props = $props();

  const MCP_PRESETS = [
    {
      label: 'Filesystem',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/path/to/dir'],
      hint: 'Replace /path/to/dir with the allowed directory path'
    },
    {
      label: 'Fetch',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-fetch'],
      hint: 'HTTP fetch tool for web requests'
    },
  ];

  let formName = $state(initialData?.name ?? '');
  let formId = $state(initialData?.id ?? '');
  let idManuallyEdited = $state(mode === 'edit');
  let formDescription = $state(initialData?.description ?? '');
  let formCommand = $state(initialData?.server_command ?? '');
  let formArgs = $state<string[]>(initialData?.server_args?.length ? [...initialData.server_args] : ['']);
  let formEnvVars = $state(
    initialData?.env_vars
      ? Object.entries(initialData.env_vars).map(([k, v]) => `${k}=${v}`).join('\n')
      : ''
  );
  let touched = $state(false);

  $effect(() => {
    if (mode === 'add' && !idManuallyEdited && formName) {
      formId = formName
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '');
    }
  });

  let formErrors = $derived.by(() => {
    const errors: Record<string, string> = {};
    if (touched || mode === 'edit') {
      if (!formId.trim()) {
        errors.id = 'Give the server an ID, like "github".';
      } else if (!/^[a-z0-9][a-z0-9_-]*$/.test(formId.trim())) {
        errors.id = 'IDs use lowercase letters, numbers, hyphens, and underscores, like "github".';
      }
      if (!formName.trim()) errors.name = 'Give the server a display name, like "GitHub".';
      if (!formCommand.trim()) errors.command = 'Enter the command that launches the server, like "npx" or "docker".';
    }
    return errors;
  });

  let hasErrors = $derived(Object.keys(formErrors).length > 0 && touched);

  function parseEnvVars(text: string): Record<string, string> {
    const result: Record<string, string> = {};
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx > 0) {
        result[trimmed.substring(0, eqIdx).trim()] = trimmed.substring(eqIdx + 1).trim();
      }
    }
    return result;
  }

  function addArg() {
    formArgs = [...formArgs, ''];
  }

  function removeArg(index: number) {
    formArgs = formArgs.filter((_, i) => i !== index);
    if (formArgs.length === 0) formArgs = [''];
  }

  function updateArg(index: number, value: string) {
    formArgs = formArgs.map((a, i) => i === index ? value : a);
  }

  function applyPreset(preset: typeof MCP_PRESETS[0]) {
    formCommand = preset.command;
    formArgs = [...preset.args];
    if (!formName) formName = preset.label;
  }

  function handleSubmit() {
    touched = true;
    if (Object.keys(formErrors).length > 0) return;
    const cleanArgs = formArgs.map(a => a.trim()).filter(a => a.length > 0);
    const envVars = parseEnvVars(formEnvVars);
    onSubmit({
      id: formId.trim(),
      name: formName.trim(),
      description: formDescription.trim() || undefined,
      server_command: formCommand.trim(),
      server_args: cleanArgs.length > 0 ? cleanArgs : undefined,
      env_vars: Object.keys(envVars).length > 0 ? envVars : undefined,
    });
  }
</script>

<div class="mcp-form">
  {#if mode === 'add'}
    <div class="presets">
      <span class="presets-label">Quick start:</span>
      {#each MCP_PRESETS as preset}
        <button type="button" class="preset-btn" onclick={() => applyPreset(preset)}>
          {preset.label}
        </button>
      {/each}
    </div>
  {/if}

  <label class="form-field" class:has-error={formErrors.name}>
    <span class="field-label">Name *</span>
    <input type="text" bind:value={formName} placeholder="e.g. Filesystem Server" onfocus={() => touched = true} />
    {#if formErrors.name}<span class="field-error"><Icon name="error" size={12} />{formErrors.name}</span>{/if}
  </label>

  <label class="form-field" class:has-error={formErrors.id}>
    <span class="field-label">
      ID *
      {#if mode === 'add' && !idManuallyEdited}
        <span class="auto-badge">auto</span>
      {/if}
    </span>
    <input
      type="text"
      bind:value={formId}
      placeholder="e.g. filesystem"
      oninput={() => { if (mode === 'add') idManuallyEdited = true; }}
      onfocus={() => touched = true}
    />
    {#if formErrors.id}<span class="field-error"><Icon name="error" size={12} />{formErrors.id}</span>{/if}
  </label>

  <label class="form-field">
    <span class="field-label">Description</span>
    <input type="text" bind:value={formDescription} placeholder="Optional description" />
  </label>

  <label class="form-field" class:has-error={formErrors.command}>
    <span class="field-label">Command *</span>
    <input type="text" bind:value={formCommand} placeholder="e.g. npx" onfocus={() => touched = true} />
    <span class="field-hint">Full path may be needed</span>
    {#if formErrors.command}<span class="field-error"><Icon name="error" size={12} />{formErrors.command}</span>{/if}
  </label>

  <div class="form-field">
    <span class="field-label">Arguments</span>
    <div class="args-list">
      {#each formArgs as arg, i}
        <div class="arg-row">
          <input
            type="text"
            value={arg}
            oninput={(e) => updateArg(i, (e.target as HTMLInputElement).value)}
            placeholder={i === 0 ? 'e.g. -y' : 'e.g. @modelcontextprotocol/...'}
          />
          <button type="button" class="arg-remove" onclick={() => removeArg(i)}>
            <Icon name="x" size={14} />
          </button>
        </div>
      {/each}
      <button type="button" class="arg-add" onclick={addArg}>
        <Icon name="plus" size={14} /> Add argument
      </button>
    </div>
  </div>

  <label class="form-field">
    <span class="field-label">Environment Variables</span>
    <textarea bind:value={formEnvVars} rows="2" placeholder="KEY=VALUE (one per line)"></textarea>
  </label>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  <div class="form-actions">
    <button class="cancel-btn" onclick={onCancel}>Cancel</button>
    <button class="submit-btn" onclick={handleSubmit} disabled={loading || hasErrors}>
      {#if loading}Adding…{:else}{mode === 'add' ? 'Add & Discover' : 'Save'}{/if}
    </button>
  </div>
</div>

<style>
  .mcp-form {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.75rem;
    background: var(--bg-elevated);
    border-radius: 8px;
  }

  .presets {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .presets-label {
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .preset-btn {
    padding: 0.25rem 0.6rem;
    font-size: 0.75rem;
    border: 1px solid var(--border-default);
    border-radius: 4px;
    background: var(--bg-base);
    color: var(--accent-primary);
    cursor: pointer;
  }

  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .field-label {
    font-size: 0.8rem;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }

  .auto-badge {
    font-size: 0.65rem;
    padding: 0 0.3rem;
    border-radius: 3px;
    background: var(--accent-primary);
    color: var(--text-on-accent);
    font-weight: 600;
  }

  .form-field input,
  .form-field textarea {
    padding: 0.5rem;
    border: 1px solid var(--border-default);
    border-radius: 4px;
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: 16px;
    font-family: inherit;
    min-height: var(--touch-target-min);
  }

  .has-error input {
    border-color: var(--error);
  }

  .field-hint {
    font-size: 0.72rem;
    color: var(--text-secondary);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .field-error {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: 0.72rem;
    color: var(--error);
  }

  .args-list {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .arg-row {
    display: flex;
    gap: 0.3rem;
    align-items: center;
  }

  .arg-row input {
    flex: 1;
    padding: 0.5rem;
    border: 1px solid var(--border-default);
    border-radius: 4px;
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: 16px;
    font-family: monospace;
    min-height: var(--touch-target-min);
  }

  .arg-remove {
    display: flex;
    padding: 0.4rem;
    border: none;
    background: none;
    color: var(--text-secondary);
  }

  .arg-add {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.4rem 0.5rem;
    border: 1px dashed var(--border-default);
    border-radius: 4px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.8rem;
    align-self: flex-start;
    min-height: var(--touch-target-min);
  }

  .form-error {
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    background: rgba(var(--error-rgb), 0.1);
    color: var(--error);
    font-size: 0.8rem;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }

  .cancel-btn {
    padding: 0.4rem 0.8rem;
    border: none;
    background: none;
    color: var(--text-secondary);
    font-size: 0.85rem;
    min-height: var(--touch-target-min);
  }

  .submit-btn {
    padding: 0.4rem 0.8rem;
    border: none;
    border-radius: 4px;
    background: var(--accent-primary);
    color: var(--text-on-accent);
    font-size: 0.85rem;
    min-height: var(--touch-target-min);
  }

  .submit-btn:disabled {
    opacity: 0.5;
  }
</style>
