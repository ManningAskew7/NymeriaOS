<script lang="ts">
  import type { MCPServerCreateRequest } from '$lib/types';
  import Button from '../common/Button.svelte';
  import Icon from '../common/Icon.svelte';

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

  // Form state
  function getInitialName(): string {
    return initialData?.name ?? '';
  }

  function getInitialId(): string {
    return initialData?.id ?? '';
  }

  function getInitialIdEdited(): boolean {
    return mode === 'edit';
  }

  function getInitialDescription(): string {
    return initialData?.description ?? '';
  }

  function getInitialCommand(): string {
    return initialData?.server_command ?? '';
  }

  function getInitialArgs(): string[] {
    return initialData?.server_args?.length ? [...initialData.server_args] : [''];
  }

  function getInitialEnvVars(): string {
    return initialData?.env_vars
      ? Object.entries(initialData.env_vars).map(([k, v]) => `${k}=${v}`).join('\n')
      : '';
  }

  let formName = $state(getInitialName());
  let formId = $state(getInitialId());
  let idManuallyEdited = $state(getInitialIdEdited());
  let formDescription = $state(getInitialDescription());
  let formCommand = $state(getInitialCommand());
  let formArgs = $state<string[]>(getInitialArgs());
  let formEnvVars = $state(getInitialEnvVars());
  let touched = $state(false);

  // Auto-generate ID from name
  $effect(() => {
    if (mode === 'add' && !idManuallyEdited && formName) {
      formId = formName
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '');
    }
  });

  // Validation
  let formErrors = $derived.by(() => {
    const errors: Record<string, string> = {};
    if (touched || mode === 'edit') {
      if (!formId.trim()) {
        errors.id = 'ID is required';
      } else if (!/^[a-z0-9][a-z0-9_-]*$/.test(formId.trim())) {
        errors.id = 'ID must be lowercase letters, numbers, hyphens, underscores';
      }
      if (!formName.trim()) errors.name = 'Name is required';
      if (!formCommand.trim()) errors.command = 'Command is required';
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
        <button type="button" class="preset-btn" onclick={() => applyPreset(preset)} title={preset.hint}>
          {preset.label}
        </button>
      {/each}
    </div>
  {/if}

  <div class="form-row">
    <label class="form-field" class:has-error={formErrors.name}>
      <span class="field-label">Name <span class="required">*</span></span>
      <input
        type="text"
        bind:value={formName}
        placeholder="e.g. Filesystem Server"
        onfocus={() => touched = true}
        aria-invalid={!!formErrors.name}
        aria-describedby={formErrors.name ? 'mcp-name-err' : undefined}
      />
      {#if formErrors.name}
        <span id="mcp-name-err" class="field-error">{formErrors.name}</span>
      {/if}
    </label>
    <label class="form-field form-field-id" class:has-error={formErrors.id}>
      <span class="field-label">
        ID <span class="required">*</span>
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
        aria-invalid={!!formErrors.id}
        aria-describedby={formErrors.id ? 'mcp-id-err' : undefined}
      />
      {#if formErrors.id}
        <span id="mcp-id-err" class="field-error">{formErrors.id}</span>
      {/if}
    </label>
  </div>

  <label class="form-field">
    <span class="field-label">Description</span>
    <input type="text" bind:value={formDescription} placeholder="Optional description" />
  </label>

  <label class="form-field" class:has-error={formErrors.command}>
    <span class="field-label">Command <span class="required">*</span></span>
    <input
      type="text"
      bind:value={formCommand}
      placeholder="e.g. npx"
      onfocus={() => touched = true}
      aria-invalid={!!formErrors.command}
      aria-describedby={formErrors.command ? 'mcp-command-err mcp-command-hint' : 'mcp-command-hint'}
    />
    <span id="mcp-command-hint" class="field-hint">Full path may be needed (e.g. C:/Program Files/nodejs/npx.cmd)</span>
    {#if formErrors.command}
      <span id="mcp-command-err" class="field-error">{formErrors.command}</span>
    {/if}
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
            placeholder={i === 0 ? 'e.g. -y' : 'e.g. @modelcontextprotocol/server-filesystem'}
          />
          <button type="button" class="arg-remove" onclick={() => removeArg(i)} title="Remove argument">
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
    <span class="field-hint">One KEY=VALUE per line. Lines starting with # are ignored.</span>
  </label>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  <div class="form-actions">
    <Button size="sm" variant="ghost" onclick={onCancel}>Cancel</Button>
    <Button size="sm" variant="primary" onclick={handleSubmit} disabled={loading || hasErrors} {loading}>
      {mode === 'add' ? 'Add & Discover Tools' : 'Save & Rediscover'}
    </Button>
  </div>
</div>

<style>
  .mcp-form {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 1rem;
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
  }

  .presets {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .presets-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .preset-btn {
    padding: 0.2rem 0.6rem;
    font-size: var(--font-size-xs);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-base);
    color: var(--accent-primary);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .preset-btn:hover {
    background: var(--bg-hover);
    border-color: var(--accent-primary);
  }

  .form-row {
    display: flex;
    gap: 0.6rem;
  }

  .form-row .form-field {
    flex: 1;
  }

  .form-field-id {
    max-width: 200px;
  }

  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .field-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }

  .required {
    color: var(--error);
  }

  .auto-badge {
    font-size: var(--font-size-3xs);
    padding: 0 0.3rem;
    border-radius: 3px;
    background: var(--accent-primary);
    color: var(--bg-base);
    font-weight: 600;
  }

  .form-field input,
  .form-field textarea {
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: inherit;
    transition: border-color var(--transition-fast);
  }

  .form-field input:focus,
  .form-field textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .has-error input {
    border-color: var(--error);
  }

  .field-hint {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .field-error {
    font-size: var(--font-size-2xs);
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
    padding: 0.35rem 0.6rem;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: var(--font-mono);
  }

  .arg-row input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .arg-remove {
    display: flex;
    align-items: center;
    padding: 0.3rem;
    border: none;
    background: none;
    color: var(--text-muted);
    cursor: pointer;
    border-radius: var(--radius-sm);
  }

  .arg-remove:hover {
    color: var(--error);
    background: var(--bg-hover);
  }

  .arg-add {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.3rem 0.5rem;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-sm);
    background: none;
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    cursor: pointer;
    align-self: flex-start;
  }

  .arg-add:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .form-error {
    padding: 0.4rem 0.6rem;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    color: var(--error);
    font-size: var(--font-size-xs);
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 0.25rem;
  }
</style>
