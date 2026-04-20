<script lang="ts">
  import type { Trigger, TriggerCreateRequest, TriggerUpdateRequest, TriggerActionType, TriggerSourceInfo } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';

  interface Props {
    threadId: string;
  }

  let { threadId }: Props = $props();

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

  // Load triggers and sources
  $effect(() => {
    if (!triggersStore.loaded && !triggersStore.loading) {
      triggersStore.loadTriggers();
    }
    if (Object.keys(triggersStore.sources).length === 0) {
      triggersStore.loadSources();
    }
  });

  // Derived
  const threadTriggers = $derived(
    triggersStore.triggers.filter(t => t.thread_id === threadId)
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

  // Labels
  function sourceLabel(type: string): string {
    const labels: Record<string, string> = { webhook: 'Webhook', outlook_email: 'Outlook Email' };
    return labels[type] || type;
  }

  function actionLabel(type: string): string {
    const labels: Record<string, string> = { agent_prompt: 'Agent Prompt', notify: 'Notification', create_todo: 'Create TODO' };
    return labels[type] || type;
  }

  function fieldLabel(key: string): string {
    return key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
  }

  function formatLastFired(iso: string | null): string {
    if (!iso) return 'Never';
    try {
      const d = new Date(iso);
      const diffMs = Date.now() - d.getTime();
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 1) return 'Just now';
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffH = Math.floor(diffMin / 60);
      if (diffH < 24) return `${diffH}h ago`;
      return `${Math.floor(diffH / 24)}d ago`;
    } catch { return iso; }
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
    if (currentSourceInfo) {
      for (const [key, schema] of Object.entries(currentSourceInfo.config_schema)) {
        if (schema.required && !formSourceConfig[key]) {
          return `${fieldLabel(key)} is required`;
        }
      }
    }
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
    if (validationError) { error = validationError; return; }
    saving = true;
    error = '';
    try {
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

<div class="trigger-config">
  {#if view === 'list'}
    <!-- List View -->
    <div class="list-header">
      <p class="list-desc">Automate actions when events occur.</p>
      <button class="new-trigger-btn" onclick={openCreate} type="button">
        <Icon name="plus" size={16} />
        <span>New Trigger</span>
      </button>
    </div>

    {#if triggersStore.loading}
      <div class="empty-state">Loading triggers...</div>
    {:else if threadTriggers.length === 0}
      <div class="empty-state">No triggers for this thread. Create one to automate tasks.</div>
    {:else}
      <div class="trigger-list">
        {#each threadTriggers as trigger (trigger.id)}
          <div class="trigger-row" class:disabled={!trigger.enabled}>
            <div class="trigger-info">
              <div class="trigger-name-row">
                <Icon name="bolt" size={14} />
                <span class="trigger-name">{trigger.name}</span>
              </div>
              <div class="trigger-meta">
                <span class="meta-tag source">{sourceLabel(trigger.source_type)}</span>
                <span class="meta-tag action">{actionLabel(trigger.action.type)}</span>
                {#if trigger.fire_count > 0}
                  <span class="meta-detail">{trigger.fire_count} fires</span>
                {/if}
                <span class="meta-detail">{formatLastFired(trigger.last_fired)}</span>
              </div>
            </div>

            <div class="trigger-actions">
              {#if confirmDeleteId === trigger.id}
                <span class="confirm-text">Delete?</span>
                <button class="action-btn danger" onclick={() => handleDelete(trigger.id)} type="button">
                  <Icon name="check" size={16} />
                </button>
                <button class="action-btn" onclick={() => (confirmDeleteId = null)} type="button">
                  <Icon name="x" size={16} />
                </button>
              {:else}
                <button class="action-btn" onclick={() => openEdit(trigger)} type="button">
                  <Icon name="edit" size={16} />
                </button>
                <button class="action-btn danger" onclick={() => (confirmDeleteId = trigger.id)} type="button">
                  <Icon name="trash" size={16} />
                </button>
              {/if}
              <button
                class="toggle-btn"
                class:off={!trigger.enabled}
                onclick={() => handleToggle(trigger)}
                type="button"
              >
                <span class="toggle-track"><span class="toggle-thumb"></span></span>
              </button>
            </div>
          </div>
        {/each}
      </div>
    {/if}

  {:else}
    <!-- Create / Edit Form -->
    <div class="form-header">
      <button class="back-link" onclick={closeForm} type="button">
        <Icon name="chevronLeft" size={18} />
        <span>Back</span>
      </button>
      <span class="form-title">{view === 'create' ? 'New Trigger' : 'Edit Trigger'}</span>
    </div>

    <div class="form-body">
      <!-- Name -->
      <div class="field-group">
        <label class="field-label">Name <span class="required">*</span></label>
        <input
          class="field-input"
          type="text"
          bind:value={formName}
          placeholder="e.g. Inbox Monitor, Deploy Webhook"
          maxlength={200}
        />
      </div>

      <!-- Source Type -->
      <div class="field-group">
        <label class="field-label">Source Type <span class="required">*</span></label>
        <p class="field-hint">What event triggers this automation?</p>
        <select
          class="field-input"
          bind:value={formSourceType}
          onchange={() => { formSourceConfig = {}; }}
        >
          <option value="">Select a source...</option>
          {#each sourceNames as name}
            <option value={name}>{sourceLabel(name)}</option>
          {/each}
        </select>
        {#if currentSourceInfo}
          <p class="field-hint">{currentSourceInfo.description}</p>
        {/if}
      </div>

      <!-- Dynamic Source Config -->
      {#if currentSourceInfo && Object.keys(currentSourceInfo.config_schema).length > 0}
        <div class="config-section">
          <span class="section-label">Source Configuration</span>
          {#each Object.entries(currentSourceInfo.config_schema) as [key, schema]}
            <div class="field-group">
              <label class="field-label">
                {fieldLabel(key)}
                {#if schema.required}<span class="required">*</span>{/if}
              </label>
              <p class="field-hint">{schema.description}</p>
              {#if schema.type === 'boolean'}
                <button
                  class="toggle-btn"
                  class:off={!formSourceConfig[key]}
                  onclick={() => handleSourceConfigChange(key, !formSourceConfig[key])}
                  type="button"
                >
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </button>
              {:else if schema.type === 'integer' || schema.type === 'number'}
                <input
                  class="field-input"
                  type="number"
                  value={(formSourceConfig[key] ?? '') as string | number}
                  oninput={(e) => handleSourceConfigChange(key, parseInt((e.target as HTMLInputElement).value) || undefined)}
                />
              {:else}
                <input
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
        <label class="field-label">Action Type <span class="required">*</span></label>
        <p class="field-hint">What happens when the trigger fires?</p>
        <select
          class="field-input"
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
            <label class="field-label">Prompt Template <span class="required">*</span></label>
            <p class="field-hint">The prompt sent to the agent. Use {'{variable}'} placeholders.</p>
            <textarea
              class="field-textarea"
              value={(formActionConfig.prompt_template ?? formActionConfig.prompt ?? '') as string}
              oninput={(e) => handleActionConfigChange('prompt_template', (e.target as HTMLTextAreaElement).value)}
              placeholder="New email received. From: {'{from_name}'}&#10;Subject: {'{subject}'}&#10;Triage this email..."
              rows={5}
            ></textarea>
          </div>

        {:else if formActionType === 'notify'}
          <div class="field-group">
            <label class="field-label">Message Template <span class="required">*</span></label>
            <textarea
              class="field-textarea"
              value={(formActionConfig.message_template ?? '') as string}
              oninput={(e) => handleActionConfigChange('message_template', (e.target as HTMLTextAreaElement).value)}
              placeholder="New email from {'{from_name}'}: {'{subject}'}"
              rows={3}
            ></textarea>
          </div>
          <div class="field-group">
            <label class="field-label">Platform</label>
            <input
              class="field-input"
              type="text"
              value={(formActionConfig.platform ?? '') as string}
              oninput={(e) => handleActionConfigChange('platform', (e.target as HTMLInputElement).value || undefined)}
              placeholder="auto (default)"
            />
          </div>

        {:else if formActionType === 'create_todo'}
          <div class="field-group">
            <label class="field-label">Task Template <span class="required">*</span></label>
            <textarea
              class="field-textarea"
              value={(formActionConfig.task_template ?? '') as string}
              oninput={(e) => handleActionConfigChange('task_template', (e.target as HTMLTextAreaElement).value)}
              placeholder="Review email from {'{from_name}'}: {'{subject}'}"
              rows={3}
            ></textarea>
          </div>
          <div class="field-group">
            <label class="field-label">Scheduled For</label>
            <input
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
            <div class="hints-list">
              {#each templateHints as hint}
                <code class="hint-var">{hint}</code>
              {/each}
            </div>
          </div>
        {/if}
      </div>

      <!-- Cooldown -->
      <div class="field-group">
        <label class="field-label">Cooldown (seconds)</label>
        <p class="field-hint">Minimum time between fires. 0 = no cooldown.</p>
        <input
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
      <button class="footer-btn secondary" onclick={closeForm} disabled={saving} type="button">Cancel</button>
      <button class="footer-btn primary" onclick={handleSave} disabled={saving} type="button">
        {saving ? 'Saving...' : view === 'create' ? 'Create Trigger' : 'Save Changes'}
      </button>
    </div>
  {/if}
</div>

<style>
  .trigger-config {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  /* List header */
  .list-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .list-desc {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    flex: 1;
  }

  .new-trigger-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: white;
    background: var(--accent-primary);
    border-radius: var(--radius-md);
    min-height: 40px;
    white-space: nowrap;
  }

  .new-trigger-btn:active {
    filter: brightness(0.9);
  }

  /* Trigger list */
  .trigger-list {
    display: flex;
    flex-direction: column;
  }

  .trigger-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
    min-height: 48px;
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
    color: var(--accent-primary);
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
    flex-wrap: wrap;
  }

  .meta-tag {
    display: inline-flex;
    padding: 1px 6px;
    font-size: var(--font-size-xs);
    font-weight: 500;
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }

  .meta-tag.source {
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
    color: var(--accent-primary);
  }

  .meta-tag.action {
    background: color-mix(in srgb, var(--text-muted) 15%, transparent);
    color: var(--text-muted);
  }

  .meta-detail {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .trigger-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }

  .action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: var(--radius-md);
    color: var(--text-muted);
  }

  .action-btn:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .action-btn.danger:active {
    color: var(--error);
  }

  .confirm-text {
    font-size: var(--font-size-xs);
    color: var(--error);
    white-space: nowrap;
  }

  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    font-size: var(--font-size-sm);
  }

  /* Toggle switch */
  .toggle-btn {
    flex-shrink: 0;
    padding: 0;
    background: none;
    border: none;
  }

  .toggle-track {
    display: block;
    width: 40px;
    height: 22px;
    border-radius: 11px;
    background: var(--accent-primary);
    position: relative;
    transition: background 0.2s;
  }

  .toggle-btn.off .toggle-track {
    background: var(--text-muted);
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 20px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: white;
    transition: left 0.2s;
  }

  .toggle-btn.off .toggle-thumb {
    left: 2px;
  }

  /* Form */
  .form-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
  }

  .back-link {
    display: flex;
    align-items: center;
    gap: 2px;
    color: var(--accent-primary);
    font-size: var(--font-size-sm);
    min-height: 44px;
  }

  .back-link:active {
    opacity: 0.7;
  }

  .form-title {
    font-size: var(--font-size-base);
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
    line-height: 1.4;
  }

  .required {
    color: var(--error);
  }

  .field-input {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: 16px;
    color: var(--text-primary);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    min-height: 44px;
  }

  .field-input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  select.field-input {
    appearance: none;
    -webkit-appearance: none;
  }

  .field-textarea {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: 16px;
    font-family: inherit;
    color: var(--text-primary);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    resize: vertical;
    min-height: 80px;
    line-height: 1.5;
  }

  .field-textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .config-section {
    margin-bottom: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
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

  .config-section .field-group:last-of-type {
    margin-bottom: 0;
  }

  .template-hints {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding-top: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    border-top: 1px solid var(--border-subtle);
  }

  .hints-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .hints-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .hint-var {
    font-size: 11px;
    padding: 2px 6px;
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
    color: var(--accent-primary);
    border-radius: var(--radius-sm);
    font-family: monospace;
  }

  .form-error {
    padding: var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-md);
    margin-bottom: var(--spacing-sm);
  }

  .form-footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .footer-btn {
    padding: var(--spacing-sm) var(--spacing-lg);
    font-size: var(--font-size-base);
    font-weight: 500;
    border-radius: var(--radius-md);
    min-height: 48px;
  }

  .footer-btn:disabled {
    opacity: 0.5;
  }

  .footer-btn.secondary {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-subtle);
  }

  .footer-btn.secondary:active {
    background: var(--bg-hover);
  }

  .footer-btn.primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .footer-btn.primary:active {
    filter: brightness(0.9);
  }
</style>
