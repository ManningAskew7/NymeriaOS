<script lang="ts">
  import type { Thread, Trigger, TriggerCreateRequest, TriggerUpdateRequest, TriggerActionType, TriggerSourceInfo } from '$lib/types';
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';

  interface Props {
    thread: Thread;
  }

  let { thread }: Props = $props();

  // View state
  let view = $state<'list' | 'create' | 'edit'>('list');
  let editingTrigger = $state<Trigger | null>(null);
  let saving = $state(false);
  let error = $state('');
  let confirmDeleteId = $state<string | null>(null);

  // Form state
  let formName = $state('');
  let formSourceType = $state('');
  let formSourceConfig = $state<Record<string, unknown>>({});
  let formActionType = $state<TriggerActionType>('agent_prompt');
  let formActionConfig = $state<Record<string, unknown>>({});
  let formCooldown = $state(0);
  let formEnabled = $state(true);

  // Load triggers and sources on mount
  $effect(() => {
    if (!triggersStore.loaded && !triggersStore.loading) {
      triggersStore.loadTriggers();
    }
    if (Object.keys(triggersStore.sources).length === 0) {
      triggersStore.loadSources();
    }
  });

  // Derived — only show triggers belonging to this thread
  const threadTriggers = $derived(
    triggersStore.triggers.filter(t => t.thread_id === thread.id)
  );
  const sourceNames = $derived(Object.keys(triggersStore.sources));
  const currentSourceInfo = $derived<TriggerSourceInfo | null>(
    formSourceType ? triggersStore.sources[formSourceType] ?? null : null
  );

  // Template variable hints per source type
  const TEMPLATE_VARS: Record<string, string[]> = {
    webhook: ['{fired_at}', '{source_ip}', '+ any JSON keys from webhook payload'],
    outlook_email: [
      '{subject}', '{from_name}', '{from_address}', '{to_addresses}',
      '{body_preview}', '{received_time}', '{is_read}', '{has_attachments}',
      '{is_flagged}', '{email_id}', '{conversation_id}', '{web_link}'
    ],
  };

  const templateHints = $derived(
    formSourceType ? (TEMPLATE_VARS[formSourceType] ?? ['{trigger_name}', '{fired_at}']) : []
  );

  // Friendly labels
  function sourceLabel(type: string): string {
    const labels: Record<string, string> = {
      webhook: 'Webhook',
      outlook_email: 'Outlook Email',
    };
    return labels[type] || type;
  }

  function actionLabel(type: string): string {
    const labels: Record<string, string> = {
      agent_prompt: 'Agent Prompt',
      notify: 'Notification',
      create_todo: 'Create TODO',
    };
    return labels[type] || type;
  }

  function fieldLabel(key: string): string {
    return key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
  }

  function formatLastFired(iso: string | null): string {
    if (!iso) return 'Never';
    try {
      const d = new Date(iso);
      const now = new Date();
      const diffMs = now.getTime() - d.getTime();
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 1) return 'Just now';
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffH = Math.floor(diffMin / 60);
      if (diffH < 24) return `${diffH}h ago`;
      const diffD = Math.floor(diffH / 24);
      return `${diffD}d ago`;
    } catch {
      return iso;
    }
  }

  // Form helpers
  function resetForm() {
    formName = '';
    formSourceType = '';
    formSourceConfig = {};
    formActionType = 'agent_prompt';
    formActionConfig = {};
    formCooldown = 0;
    formEnabled = true;
    error = '';
  }

  function openCreate() {
    resetForm();
    view = 'create';
  }

  function openEdit(trigger: Trigger) {
    editingTrigger = trigger;
    formName = trigger.name;
    formSourceType = trigger.source_type;
    formSourceConfig = { ...trigger.source_config };
    formActionType = trigger.action.type;
    formActionConfig = { ...trigger.action.config };
    formCooldown = trigger.cooldown_seconds;
    formEnabled = trigger.enabled;
    error = '';
    view = 'edit';
  }

  function closeForm() {
    view = 'list';
    editingTrigger = null;
    error = '';
  }

  function handleSourceConfigChange(key: string, value: unknown) {
    formSourceConfig = { ...formSourceConfig, [key]: value };
  }

  function handleActionConfigChange(key: string, value: unknown) {
    formActionConfig = { ...formActionConfig, [key]: value };
  }

  // Validation
  function validate(): string | null {
    if (!formName.trim()) return 'Trigger name is required';
    if (formName.length > 200) return 'Name must be under 200 characters';
    if (!formSourceType) return 'Source type is required';
    if (!formActionType) return 'Action type is required';

    // Check required source config fields
    if (currentSourceInfo) {
      for (const [key, schema] of Object.entries(currentSourceInfo.config_schema)) {
        if (schema.required && !formSourceConfig[key]) {
          return `${fieldLabel(key)} is required`;
        }
      }
    }

    // Check action-specific required fields
    if (formActionType === 'agent_prompt' && !formActionConfig.prompt_template && !formActionConfig.prompt) {
      return 'Prompt template is required';
    }
    if (formActionType === 'notify' && !formActionConfig.message_template) {
      return 'Message template is required';
    }
    if (formActionType === 'create_todo' && !formActionConfig.task_template) {
      return 'Task template is required';
    }

    return null;
  }

  async function handleSave() {
    const validationError = validate();
    if (validationError) {
      error = validationError;
      return;
    }

    saving = true;
    error = '';

    try {
      // Normalize: use prompt_template key for agent_prompt actions
      const actionConfig = { ...formActionConfig };
      if (formActionType === 'agent_prompt' && actionConfig.prompt && !actionConfig.prompt_template) {
        actionConfig.prompt_template = actionConfig.prompt;
        delete actionConfig.prompt;
      }

      if (view === 'create') {
        const request: TriggerCreateRequest = {
          name: formName.trim(),
          source_type: formSourceType,
          source_config: formSourceConfig,
          action_type: formActionType,
          action_config: actionConfig,
          cooldown_seconds: formCooldown,
          enabled: formEnabled,
          thread_id: thread.id,
        };
        await triggersStore.createTrigger(request);
      } else if (view === 'edit' && editingTrigger) {
        const request: TriggerUpdateRequest = {
          name: formName.trim(),
          source_config: formSourceConfig,
          action_type: formActionType,
          action_config: actionConfig,
          cooldown_seconds: formCooldown,
        };
        await triggersStore.updateTrigger(editingTrigger.id, request);
      }
      closeForm();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save trigger';
    } finally {
      saving = false;
    }
  }

  async function handleToggle(trigger: Trigger) {
    try {
      await triggersStore.updateTrigger(trigger.id, { enabled: !trigger.enabled });
    } catch (e) {
      console.error('Failed to toggle trigger:', e);
    }
  }

  async function handleDelete(id: string) {
    try {
      await triggersStore.deleteTrigger(id);
      confirmDeleteId = null;
    } catch (e) {
      console.error('Failed to delete trigger:', e);
    }
  }
</script>

{#if view === 'list'}
  <!-- List View -->
  <div class="trigger-list-header">
    <p class="field-hint" style="margin: 0;">
      Automate actions when events occur (new emails, webhooks, etc.).
    </p>
    <button class="btn btn-primary btn-sm" onclick={openCreate} type="button">
      <Icon name="plus" size={14} /> New Trigger
    </button>
  </div>

  {#if triggersStore.loading}
    <div class="empty-state">Loading triggers...</div>
  {:else if threadTriggers.length === 0}
    <div class="empty-state">
      No triggers configured for this thread. Create one to automate tasks.
    </div>
  {:else}
    <div class="triggers-list">
      {#each threadTriggers as trigger (trigger.id)}
        <div class="trigger-row" class:disabled={!trigger.enabled}>
          <div class="trigger-info">
            <div class="trigger-name-row">
              <Icon name="bolt" size={14} class="trigger-icon" />
              <span class="trigger-name">{trigger.name}</span>
            </div>
            <div class="trigger-meta">
              <span class="meta-tag source" data-source={trigger.source_type}>{sourceLabel(trigger.source_type)}</span>
              <span class="meta-tag action" data-action={trigger.action.type}>{actionLabel(trigger.action.type)}</span>
              {#if trigger.fire_count > 0}
                <span class="meta-detail">{trigger.fire_count} fires</span>
              {/if}
              <span class="meta-detail">{formatLastFired(trigger.last_fired)}</span>
            </div>
          </div>

          <div class="trigger-actions">
            {#if confirmDeleteId === trigger.id}
              <span class="confirm-text">Delete?</span>
              <button
                class="icon-btn danger"
                onclick={() => handleDelete(trigger.id)}
                title="Confirm delete"
                type="button"
              >
                <Icon name="check" size={14} />
              </button>
              <button
                class="icon-btn"
                onclick={() => (confirmDeleteId = null)}
                title="Cancel"
                type="button"
              >
                <Icon name="x" size={14} />
              </button>
            {:else}
              <button
                class="icon-btn"
                onclick={() => openEdit(trigger)}
                title="Edit trigger"
                type="button"
              >
                <Icon name="edit" size={14} />
              </button>
              <button
                class="icon-btn danger"
                onclick={() => (confirmDeleteId = trigger.id)}
                title="Delete trigger"
                type="button"
              >
                <Icon name="trash" size={14} />
              </button>
            {/if}

            <ToggleSwitch
              checked={trigger.enabled}
              onclick={() => handleToggle(trigger)}
              title={trigger.enabled ? 'Disable trigger' : 'Enable trigger'}
              ariaLabel={trigger.enabled ? 'Disable trigger' : 'Enable trigger'}
            />
          </div>
        </div>
      {/each}
    </div>
  {/if}

{:else}
  <!-- Create / Edit Form -->
  <div class="form-header">
    <button class="icon-btn" onclick={closeForm} title="Back to list" type="button" aria-label="Back to list">
      <Icon name="chevronRight" size={16} class="back-icon" />
    </button>
    <span class="form-title">{view === 'create' ? 'New Trigger' : 'Edit Trigger'}</span>
  </div>

  <div class="form-body">
    <!-- Name -->
    <div class="field-group">
      <label class="field-label" for="trigger-name">Name <span class="required">*</span></label>
      <input
        id="trigger-name"
        class="field-input"
        type="text"
        bind:value={formName}
        placeholder="e.g. Inbox Monitor, Deploy Webhook"
        maxlength={200}
      />
    </div>

    <!-- Source Type -->
    <div class="field-group">
      <label class="field-label" for="trigger-source">Source Type <span class="required">*</span></label>
      <p class="field-hint">What event triggers this automation?</p>
      <select
        id="trigger-source"
        class="field-select"
        bind:value={formSourceType}
        onchange={() => { formSourceConfig = {}; }}
      >
        <option value="">Select a source...</option>
        {#each sourceNames as name}
          <option value={name}>{sourceLabel(name)}</option>
        {/each}
      </select>
      {#if currentSourceInfo}
        <p class="field-hint" style="margin-top: 4px;">{currentSourceInfo.description}</p>
      {/if}
    </div>

    <!-- Dynamic Source Config -->
    {#if currentSourceInfo && Object.keys(currentSourceInfo.config_schema).length > 0}
      <div class="config-section">
        <span class="section-label">Source Configuration</span>
        {#each Object.entries(currentSourceInfo.config_schema) as [key, schema]}
          <div class="field-group">
            <label class="field-label" for="src-{key}">
              {fieldLabel(key)}
              {#if schema.required}<span class="required">*</span>{/if}
            </label>
            <p class="field-hint">{schema.description}</p>
            {#if schema.type === 'boolean'}
              <ToggleSwitch
                checked={Boolean(formSourceConfig[key])}
                onclick={() => handleSourceConfigChange(key, !formSourceConfig[key])}
                ariaLabel={fieldLabel(key)}
              />
            {:else if schema.type === 'integer' || schema.type === 'number'}
              <input
                id="src-{key}"
                class="field-input"
                type="number"
                value={(formSourceConfig[key] ?? '') as string | number}
                oninput={(e) => handleSourceConfigChange(key, parseInt((e.target as HTMLInputElement).value) || undefined)}
              />
            {:else}
              <input
                id="src-{key}"
                class="field-input"
                type="text"
                value={(formSourceConfig[key] ?? '') as string}
                oninput={(e) => handleSourceConfigChange(key, (e.target as HTMLInputElement).value || undefined)}
              />
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    <!-- Action Type -->
    <div class="field-group">
      <label class="field-label" for="trigger-action">Action Type <span class="required">*</span></label>
      <p class="field-hint">What happens when the trigger fires?</p>
      <select
        id="trigger-action"
        class="field-select"
        bind:value={formActionType}
        onchange={() => { formActionConfig = {}; }}
      >
        <option value="agent_prompt">Agent Prompt (LLM call)</option>
        <option value="notify">Notification (no LLM)</option>
        <option value="create_todo">Create TODO (no LLM)</option>
      </select>
    </div>

    <!-- Action Config -->
    <div class="config-section">
      <span class="section-label">Action Configuration</span>

      {#if formActionType === 'agent_prompt'}
        <div class="field-group">
          <label class="field-label" for="action-prompt">
            Prompt Template <span class="required">*</span>
          </label>
          <p class="field-hint">
            The prompt sent to the agent when this trigger fires. Use {'{variable}'} placeholders.
          </p>
          <textarea
            id="action-prompt"
            class="field-textarea"
            value={(formActionConfig.prompt_template ?? formActionConfig.prompt ?? '') as string}
            oninput={(e) => handleActionConfigChange('prompt_template', (e.target as HTMLTextAreaElement).value)}
            placeholder="New email received. From: {'{from_name}'} ({'{from_address}'})\nSubject: {'{subject}'}\nPreview: {'{body_preview}'}\n\nTriage this email..."
            rows={5}
          ></textarea>
        </div>

      {:else if formActionType === 'notify'}
        <div class="field-group">
          <label class="field-label" for="action-message">
            Message Template <span class="required">*</span>
          </label>
          <textarea
            id="action-message"
            class="field-textarea"
            value={(formActionConfig.message_template ?? '') as string}
            oninput={(e) => handleActionConfigChange('message_template', (e.target as HTMLTextAreaElement).value)}
            placeholder="New email from {'{from_name}'}: {'{subject}'}"
            rows={3}
          ></textarea>
        </div>
        <div class="field-group">
          <label class="field-label" for="action-platform">Platform</label>
          <input
            id="action-platform"
            class="field-input"
            type="text"
            value={(formActionConfig.platform ?? '') as string}
            oninput={(e) => handleActionConfigChange('platform', (e.target as HTMLInputElement).value || undefined)}
            placeholder="auto (default)"
          />
        </div>

      {:else if formActionType === 'create_todo'}
        <div class="field-group">
          <label class="field-label" for="action-task">
            Task Template <span class="required">*</span>
          </label>
          <textarea
            id="action-task"
            class="field-textarea"
            value={(formActionConfig.task_template ?? '') as string}
            oninput={(e) => handleActionConfigChange('task_template', (e.target as HTMLTextAreaElement).value)}
            placeholder="Review email from {'{from_name}'}: {'{subject}'}"
            rows={3}
          ></textarea>
        </div>
        <div class="field-group">
          <label class="field-label" for="action-schedule">Scheduled For</label>
          <input
            id="action-schedule"
            class="field-input"
            type="text"
            value={(formActionConfig.scheduled_for ?? '') as string}
            oninput={(e) => handleActionConfigChange('scheduled_for', (e.target as HTMLInputElement).value || undefined)}
            placeholder='Relative (e.g. "2h", "30m") or ISO datetime'
          />
        </div>
      {/if}

      <!-- Template variable hints -->
      {#if templateHints.length > 0}
        <div class="template-hints">
          <span class="hints-label">Available variables:</span>
          <span class="hints-list">
            {#each templateHints as hint, i}
              <code class="hint-var">{hint}</code>{#if i < templateHints.length - 1}{' '}{/if}
            {/each}
          </span>
        </div>
      {/if}
    </div>

    <!-- Cooldown -->
    <div class="field-group">
      <label class="field-label" for="trigger-cooldown">Cooldown (seconds)</label>
      <p class="field-hint">Minimum time between trigger fires. 0 = no cooldown.</p>
      <input
        id="trigger-cooldown"
        class="field-input"
        type="number"
        min={0}
        bind:value={formCooldown}
      />
    </div>

    {#if error}
      <div class="form-error">{error}</div>
    {/if}
  </div>

  <div class="form-footer">
    <button class="btn btn-ghost" onclick={closeForm} disabled={saving} type="button">
      Cancel
    </button>
    <button class="btn btn-primary" onclick={handleSave} disabled={saving} type="button">
      {saving ? 'Saving...' : view === 'create' ? 'Create Trigger' : 'Save Changes'}
    </button>
  </div>
{/if}

<style>
  /* List header */
  .trigger-list-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-sm);
  }

  .btn-sm {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    font-size: var(--font-size-xs);
  }

  /* Triggers list */
  .triggers-list {
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    max-height: 380px;
    overflow-y: auto;
  }

  .trigger-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle, var(--border-default));
    transition: opacity var(--transition-fast);
  }

  .trigger-row:last-child {
    border-bottom: none;
  }

  .trigger-row.disabled {
    opacity: 0.5;
  }

  .trigger-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .trigger-name-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .trigger-name-row :global(.trigger-icon) {
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .trigger-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .trigger-row.disabled .trigger-name {
    text-decoration: line-through;
  }

  .trigger-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    padding-left: 20px;
  }

  .meta-tag {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* Gap matches horizontal padding so the leading dot has equal
       breathing on both sides of itself. */
    gap: 5px;
    padding: 1px 5px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    border-radius: var(--radius-md);
    white-space: nowrap;
  }

  /* Per-source / per-action leading dot — differentiates trigger rows at
     a glance instead of two near-identical pills per trigger. */
  .meta-tag::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: var(--radius-full);
    background: var(--chip-dot-color, var(--text-muted));
    flex-shrink: 0;
  }

  .meta-tag.source {
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
    color: var(--accent-primary);
  }

  .meta-tag.action {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
  }

  /* Source-type dot colors — kept aligned with .source-chip in
     TriggerItem.svelte so the same source reads the same color across
     both views. */
  .meta-tag.source[data-source="webhook"] { --chip-dot-color: var(--warning, #fbbf24); }
  .meta-tag.source[data-source="outlook_email"] { --chip-dot-color: var(--info, #818cf8); }
  .meta-tag.source[data-source="rss"] { --chip-dot-color: #fb923c; }
  .meta-tag.source[data-source="slack"] { --chip-dot-color: var(--success, #34d399); }
  .meta-tag.source[data-source="teams"] { --chip-dot-color: #a78bfa; }
  .meta-tag.source[data-source="http_poll"] { --chip-dot-color: var(--accent-primary); }

  /* Action-type dot colors — semantic palette: prompt = primary accent
     (Nymeria's core AI function), notify = warning (alert), todo =
     success (concrete output). */
  .meta-tag.action[data-action="agent_prompt"] { --chip-dot-color: var(--accent-primary); }
  .meta-tag.action[data-action="notify"] { --chip-dot-color: var(--warning, #fbbf24); }
  .meta-tag.action[data-action="create_todo"] { --chip-dot-color: var(--success, #34d399); }

  .meta-detail {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .trigger-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    transition: all var(--transition-fast);
  }

  .icon-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .icon-btn.danger:hover {
    color: var(--error);
  }

  .confirm-text {
    font-size: var(--font-size-xs);
    color: var(--error);
    white-space: nowrap;
  }

  .empty-state {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
  }

  /* Form */
  .form-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-md);
  }

  .form-header :global(.back-icon) {
    transform: rotate(180deg);
  }

  .form-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .form-body {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .field-group {
    margin-bottom: var(--spacing-md);
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
    margin: 0 0 var(--spacing-xs) 0;
  }

  .required {
    color: var(--error);
  }

  .field-input,
  .field-select {
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

  .field-input:focus,
  .field-select:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .field-input::placeholder {
    color: var(--text-muted);
  }

  .field-select {
    cursor: pointer;
  }

  .field-textarea {
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
    min-height: 60px;
    transition: border-color var(--transition-fast);
  }

  .field-textarea:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .field-textarea::placeholder {
    color: var(--text-muted);
  }

  .config-section {
    margin-bottom: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg-base) 50%, transparent);
  }

  .section-label {
    display: block;
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: var(--spacing-sm);
  }

  .config-section .field-group:last-child {
    margin-bottom: 0;
  }

  .template-hints {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px;
    padding: var(--spacing-xs) 0;
    margin-top: var(--spacing-xs);
    border-top: 1px solid var(--border-default);
  }

  .hints-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
  }

  .hints-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .hint-var {
    font-size: var(--font-size-3xs);
    padding: 1px 4px;
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    color: var(--accent-primary);
    border-radius: 3px;
    font-family: var(--font-mono);
  }

  .form-error {
    padding: var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-sm);
    margin-bottom: var(--spacing-sm);
  }

  .form-footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-default);
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-ghost:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }
</style>
