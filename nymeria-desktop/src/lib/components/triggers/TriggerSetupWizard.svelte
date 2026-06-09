<script lang="ts">
  import type {
    TriggerSourceInfo,
    TriggerSourceSchemaField,
    TriggerCondition,
    TriggerActionType,
    TriggerCreateRequest,
    TriggerUpdateRequest,
    Trigger,
  } from '$lib/types';
  import { trapFocus } from '$lib/actions/focus';
  import { Icon } from '$lib/components/common';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';

  interface Props {
    threadId?: string;
    editTrigger?: Trigger;
    onClose: () => void;
    onCreated?: (trigger: Trigger) => void;
  }

  let { threadId, editTrigger, onClose, onCreated }: Props = $props();

  const isEditing = $derived(!!editTrigger);
  const resolvedThreadId = $derived(threadId || threadsStore.currentThreadId || '');

  // Wizard steps
  type WizardStep = 'source' | 'configure' | 'conditions' | 'action' | 'review';
  const steps: WizardStep[] = ['source', 'configure', 'conditions', 'action', 'review'];

  function getInitialStep(): WizardStep {
    return editTrigger ? 'configure' : 'source';
  }

  let currentStep = $state<WizardStep>(getInitialStep());
  let stepIndex = $derived(steps.indexOf(currentStep));

  // Form state
  function getInitialSelectedSource(): string {
    return editTrigger?.source_type || '';
  }

  function getInitialSourceConfig(): Record<string, unknown> {
    return editTrigger?.source_config ? { ...editTrigger.source_config } : {};
  }

  function getInitialConditions(): TriggerCondition[] {
    return editTrigger?.conditions ? [...editTrigger.conditions] : [];
  }

  function getInitialActionType(): TriggerActionType {
    return editTrigger?.action.type || 'agent_prompt';
  }

  function getInitialActionConfig(): Record<string, unknown> {
    return editTrigger?.action.config ? { ...editTrigger.action.config } : {};
  }

  function getInitialTriggerName(): string {
    return editTrigger?.name || '';
  }

  function getInitialCooldownSeconds(): number {
    return editTrigger?.cooldown_seconds || 0;
  }

  function getInitialEnabled(): boolean {
    return editTrigger?.enabled ?? true;
  }

  let selectedSource = $state<string>(getInitialSelectedSource());
  let sourceConfig = $state<Record<string, unknown>>(getInitialSourceConfig());
  let conditions = $state<TriggerCondition[]>(getInitialConditions());
  let actionType = $state<TriggerActionType>(getInitialActionType());
  let actionConfig = $state<Record<string, unknown>>(getInitialActionConfig());
  let triggerName = $state(getInitialTriggerName());
  let cooldownSeconds = $state(getInitialCooldownSeconds());
  let enabled = $state(getInitialEnabled());

  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let sourceSearch = $state('');
  let categoryFilter = $state<string | null>(null);

  // Source info
  const sourceInfo = $derived<TriggerSourceInfo | undefined>(
    selectedSource ? triggersStore.sources[selectedSource] : undefined
  );

  // Grouped & filtered sources for step 1
  const sourceList = $derived(Object.values(triggersStore.sources));
  const categories = $derived([...new Set(sourceList.map(s => s.category))].sort());
  const filteredSources = $derived(
    sourceList.filter(s => {
      if (categoryFilter && s.category !== categoryFilter) return false;
      if (sourceSearch) {
        const q = sourceSearch.toLowerCase();
        return s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q);
      }
      return true;
    })
  );

  // Config schema sorted by order
  const sortedFields = $derived<[string, TriggerSourceSchemaField][]>(
    sourceInfo
      ? Object.entries(sourceInfo.config_schema).sort(
          ([, a], [, b]) => (a.order ?? 99) - (b.order ?? 99)
        )
      : []
  );

  // Group fields by group label
  const fieldGroups = $derived.by(() => {
    const groups: Record<string, [string, TriggerSourceSchemaField][]> = {};
    for (const entry of sortedFields) {
      const group = entry[1].group || 'General';
      if (!groups[group]) groups[group] = [];
      groups[group].push(entry);
    }
    return groups;
  });

  // Template variables for selected source
  const templateVars = $derived(sourceInfo?.template_variables || []);

  // Validation
  function validateConfigure(): string | null {
    if (!sourceInfo) return 'No source selected';
    for (const [key, field] of sortedFields) {
      if (field.required && !sourceConfig[key] && sourceConfig[key] !== false && sourceConfig[key] !== 0) {
        return `${field.description || key} is required`;
      }
    }
    return null;
  }

  function validateAction(): string | null {
    if (actionType === 'agent_prompt') {
      if (!actionConfig.prompt_template) return 'Prompt template is required';
    } else if (actionType === 'notify') {
      if (!actionConfig.message_template) return 'Message template is required';
    } else if (actionType === 'create_todo') {
      if (!actionConfig.task_template) return 'Task template is required';
    }
    return null;
  }

  function canAdvance(): boolean {
    switch (currentStep) {
      case 'source': return !!selectedSource;
      case 'configure': return !validateConfigure();
      case 'conditions': return true;
      case 'action': return !validateAction();
      case 'review': return !!triggerName.trim();
      default: return false;
    }
  }

  function next() {
    const idx = stepIndex;
    if (idx < steps.length - 1) {
      currentStep = steps[idx + 1];
    }
  }

  function back() {
    const idx = stepIndex;
    if (idx > 0) {
      currentStep = steps[idx - 1];
    }
  }

  function selectSource(name: string) {
    if (selectedSource === name) return;
    selectedSource = name;
    const info = triggersStore.sources[name];
    if (info?.example_config) {
      sourceConfig = { ...info.example_config };
    } else {
      sourceConfig = {};
    }
    // Prefill defaults
    for (const [key, field] of Object.entries(info?.config_schema || {})) {
      if (field.default !== undefined && sourceConfig[key] === undefined) {
        sourceConfig[key] = field.default;
      }
    }
  }

  function addCondition() {
    conditions = [...conditions, { field: templateVars[0] || '', operator: 'contains', value: '' }];
  }

  function removeCondition(index: number) {
    conditions = conditions.filter((_, i) => i !== index);
  }

  function updateCondition(index: number, updates: Partial<TriggerCondition>) {
    conditions = conditions.map((c, i) => i === index ? { ...c, ...updates } : c);
  }

  async function handleSave() {
    if (!triggerName.trim()) { saveError = 'Name is required'; return; }
    saving = true;
    saveError = null;

    try {
      if (isEditing && editTrigger) {
        const update: TriggerUpdateRequest = {
          name: triggerName.trim(),
          source_config: sourceConfig,
          action_type: actionType,
          action_config: actionConfig,
          conditions: conditions.filter(c => c.field && c.value),
          cooldown_seconds: cooldownSeconds,
          enabled,
        };
        await triggersStore.updateTrigger(editTrigger.id, update);
      } else {
        const request: TriggerCreateRequest = {
          name: triggerName.trim(),
          source_type: selectedSource,
          source_config: sourceConfig,
          action_type: actionType,
          action_config: actionConfig,
          conditions: conditions.filter(c => c.field && c.value),
          cooldown_seconds: cooldownSeconds,
          enabled,
          thread_id: resolvedThreadId || undefined,
        };
        const created = await triggersStore.createTrigger(request);
        onCreated?.(created);
      }
      onClose();
    } catch (e) {
      saveError = e instanceof Error ? e.message : 'Failed to save trigger';
    } finally {
      saving = false;
    }
  }

  function autoName(): string {
    if (triggerName) return triggerName;
    const src = sourceInfo?.name || selectedSource;
    const action = actionType.replace('_', ' ');
    return `${src} → ${action}`;
  }

  const categoryIcons: Record<string, string> = {
    communication: 'chat',
    monitoring: 'refresh',
    developer: 'terminal',
    custom: 'bolt',
    general: 'cog',
  };

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="wizard-overlay">
  <button
    class="wizard-backdrop"
    type="button"
    tabindex="-1"
    aria-label={isEditing ? 'Close edit trigger dialog' : 'Close new trigger dialog'}
    onclick={onClose}
  ></button>
  <div class="wizard-modal" role="dialog" aria-modal="true" aria-labelledby="trigger-wizard-title" tabindex="-1" use:trapFocus>
    <!-- Header -->
    <div class="wizard-header">
      <h2 id="trigger-wizard-title">{isEditing ? 'Edit Trigger' : 'New Trigger'}</h2>
      <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
        <Icon name="x" size={16} />
      </button>
    </div>

    <!-- Progress -->
    <div class="wizard-progress">
      {#each steps as step, i}
        <button
          class="progress-step"
          class:active={i === stepIndex}
          class:completed={i < stepIndex}
          class:clickable={i < stepIndex}
          onclick={() => { if (i < stepIndex) currentStep = step; }}
          type="button"
        >
          <span class="step-number">{i + 1}</span>
          <span class="step-label">
            {step === 'source' ? 'Source' : step === 'configure' ? 'Config' : step === 'conditions' ? 'Filters' : step === 'action' ? 'Action' : 'Review'}
          </span>
        </button>
        {#if i < steps.length - 1}
          <div class="progress-line" class:filled={i < stepIndex}></div>
        {/if}
      {/each}
    </div>

    <!-- Body -->
    <div class="wizard-body">
      <!-- Step 1: Choose Source -->
      {#if currentStep === 'source'}
        <div class="step-content">
          <h3>Choose a trigger source</h3>
          <p class="step-desc">Select what event should fire this trigger</p>

          <div class="source-filters">
            <input
              class="search-input"
              type="text"
              placeholder="Search sources..."
              bind:value={sourceSearch}
            />
            <div class="category-tabs" role="tablist" aria-label="Trigger source category">
              <button
                class="cat-tab" class:active={!categoryFilter}
                onclick={() => (categoryFilter = null)} type="button"
                role="tab" aria-selected={!categoryFilter}
              >All</button>
              {#each categories as cat}
                <button
                  class="cat-tab" class:active={categoryFilter === cat}
                  onclick={() => (categoryFilter = cat)} type="button"
                  role="tab" aria-selected={categoryFilter === cat}
                >{cat}</button>
              {/each}
            </div>
          </div>

          <div class="source-grid">
            {#each filteredSources as source}
              <button
                class="source-card"
                class:selected={selectedSource === source.name}
                onclick={() => selectSource(source.name)}
                type="button"
              >
                <div class="source-card-icon">
                  <Icon name={source.icon || 'bolt'} size={18} />
                </div>
                <div class="source-card-info">
                  <span class="source-card-name">{source.name.replace('_', ' ')}</span>
                  <span class="source-card-desc">{source.description}</span>
                </div>
                {#if source.requires_auth}
                  <span class="auth-badge">auth</span>
                {/if}
              </button>
            {/each}
          </div>
        </div>

      <!-- Step 2: Configure Source -->
      {:else if currentStep === 'configure'}
        <div class="step-content">
          <h3>Configure {sourceInfo?.name.replace('_', ' ') || selectedSource}</h3>

          {#if sourceInfo?.setup_guide}
            <div class="setup-guide">{sourceInfo.setup_guide}</div>
          {/if}

          <div class="config-form">
            {#each Object.entries(fieldGroups) as [group, fields]}
              <div class="field-group">
                <h4 class="group-title">{group}</h4>
                {#each fields as [key, field]}
                  <div class="field-row">
                    <label class="field-label" for="cfg-{key}">
                      {field.description}
                      {#if field.required}<span class="required">*</span>{/if}
                    </label>
                    {#if field.type === 'boolean'}
                      <label class="checkbox-label">
                        <input
                          type="checkbox"
                          checked={!!sourceConfig[key]}
                          onchange={(e) => { sourceConfig[key] = (e.target as HTMLInputElement).checked; sourceConfig = sourceConfig; }}
                        />
                        <span>{field.description}</span>
                      </label>
                    {:else if field.enum}
                      <select
                        id="cfg-{key}"
                        class="field-select"
                        value={String(sourceConfig[key] ?? field.default ?? '')}
                        onchange={(e) => { sourceConfig[key] = (e.target as HTMLSelectElement).value; sourceConfig = sourceConfig; }}
                      >
                        {#each field.enum as option}
                          <option value={option}>{option}</option>
                        {/each}
                      </select>
                    {:else if field.type === 'integer' || field.type === 'number'}
                      <input
                        id="cfg-{key}"
                        class="field-input"
                        type="number"
                        placeholder={field.placeholder || ''}
                        value={sourceConfig[key] ?? field.default ?? ''}
                        oninput={(e) => { sourceConfig[key] = Number((e.target as HTMLInputElement).value); sourceConfig = sourceConfig; }}
                      />
                    {:else}
                      <input
                        id="cfg-{key}"
                        class="field-input"
                        type={field.secret ? 'password' : 'text'}
                        placeholder={field.placeholder || ''}
                        value={String(sourceConfig[key] ?? '')}
                        oninput={(e) => { sourceConfig[key] = (e.target as HTMLInputElement).value; sourceConfig = sourceConfig; }}
                      />
                    {/if}
                  </div>
                {/each}
              </div>
            {/each}
          </div>
        </div>

      <!-- Step 3: Conditions -->
      {:else if currentStep === 'conditions'}
        <div class="step-content">
          <h3>Filter conditions <span class="optional-badge">optional</span></h3>
          <p class="step-desc">Only fire when ALL conditions match</p>

          {#if conditions.length === 0}
            <p class="no-conditions">No conditions set; trigger fires on every event</p>
          {/if}

          {#each conditions as condition, i}
            <div class="condition-row">
              <select
                class="condition-field"
                value={condition.field}
                onchange={(e) => updateCondition(i, { field: (e.target as HTMLSelectElement).value })}
              >
                {#each templateVars as v}
                  <option value={v}>{v}</option>
                {/each}
              </select>
              <select
                class="condition-op"
                value={condition.operator}
                onchange={(e) => updateCondition(i, { operator: (e.target as HTMLSelectElement).value as TriggerCondition['operator'] })}
              >
                <option value="contains">contains</option>
                <option value="equals">equals</option>
                <option value="not_equals">not equals</option>
                <option value="starts_with">starts with</option>
                <option value="matches_regex">matches regex</option>
              </select>
              <input
                class="condition-value"
                type="text"
                placeholder="value"
                value={condition.value}
                oninput={(e) => updateCondition(i, { value: (e.target as HTMLInputElement).value })}
              />
              <button class="condition-remove" onclick={() => removeCondition(i)} type="button">
                <Icon name="x" size={12} />
              </button>
            </div>
          {/each}

          <button class="add-condition-btn" onclick={addCondition} type="button">
            <Icon name="plus" size={12} />
            <span>Add condition</span>
          </button>
        </div>

      <!-- Step 4: Action -->
      {:else if currentStep === 'action'}
        <div class="step-content">
          <h3>Choose an action</h3>
          <p class="step-desc">What should happen when the trigger fires?</p>

          <div class="action-type-selector">
            <button
              class="action-type-btn" class:selected={actionType === 'agent_prompt'}
              onclick={() => (actionType = 'agent_prompt')}
              type="button"
            >
              <Icon name="chat" size={16} />
              <span class="at-name">Agent Prompt</span>
              <span class="at-desc">Send event to the AI agent for processing</span>
            </button>
            <button
              class="action-type-btn" class:selected={actionType === 'notify'}
              onclick={() => (actionType = 'notify')}
              type="button"
            >
              <Icon name="bell" size={16} />
              <span class="at-name">Notify</span>
              <span class="at-desc">Send a notification without AI processing</span>
            </button>
            <button
              class="action-type-btn" class:selected={actionType === 'create_todo'}
              onclick={() => (actionType = 'create_todo')}
              type="button"
            >
              <Icon name="check" size={16} />
              <span class="at-name">Create Task</span>
              <span class="at-desc">Add a TODO item automatically</span>
            </button>
          </div>

          <div class="action-config">
            {#if actionType === 'agent_prompt'}
              <label class="field-label" for="prompt-tpl">
                Prompt template
                <span class="required">*</span>
              </label>
              <textarea
                id="prompt-tpl"
                class="template-textarea"
                placeholder="New {sourceInfo?.name || 'event'}: {'{content}'}"
                value={String(actionConfig.prompt_template || '')}
                oninput={(e) => { actionConfig.prompt_template = (e.target as HTMLTextAreaElement).value; actionConfig = actionConfig; }}
                rows={4}
              ></textarea>
            {:else if actionType === 'notify'}
              <label class="field-label" for="msg-tpl">
                Message template
                <span class="required">*</span>
              </label>
              <textarea
                id="msg-tpl"
                class="template-textarea"
                placeholder="Notification: {'{content}'}"
                value={String(actionConfig.message_template || '')}
                oninput={(e) => { actionConfig.message_template = (e.target as HTMLTextAreaElement).value; actionConfig = actionConfig; }}
                rows={4}
              ></textarea>
              <label class="field-label" for="notify-platform">Platform</label>
              <input
                id="notify-platform"
                class="field-input"
                type="text"
                placeholder="desktop, discord, telegram..."
                value={String(actionConfig.platform || '')}
                oninput={(e) => { actionConfig.platform = (e.target as HTMLInputElement).value; actionConfig = actionConfig; }}
              />
            {:else if actionType === 'create_todo'}
              <label class="field-label" for="task-tpl">
                Task template
                <span class="required">*</span>
              </label>
              <textarea
                id="task-tpl"
                class="template-textarea"
                placeholder="Review: {'{content}'}"
                value={String(actionConfig.task_template || '')}
                oninput={(e) => { actionConfig.task_template = (e.target as HTMLTextAreaElement).value; actionConfig = actionConfig; }}
                rows={3}
              ></textarea>
            {/if}

            {#if templateVars.length > 0}
              <div class="template-hints">
                <span class="hints-label">Available variables:</span>
                {#each templateVars as v}
                  <code class="var-hint">{`{${v}}`}</code>
                {/each}
              </div>
            {/if}
          </div>
        </div>

      <!-- Step 5: Review -->
      {:else if currentStep === 'review'}
        <div class="step-content">
          <h3>Review & Save</h3>

          <div class="review-form">
            <div class="field-row">
              <label class="field-label" for="trigger-name">
                Trigger name <span class="required">*</span>
              </label>
              <input
                id="trigger-name"
                class="field-input"
                type="text"
                placeholder={autoName()}
                bind:value={triggerName}
              />
            </div>

            <div class="field-row">
              <label class="field-label" for="cooldown">
                Cooldown (seconds)
              </label>
              <input
                id="cooldown"
                class="field-input"
                type="number"
                min="0"
                bind:value={cooldownSeconds}
              />
              <span class="field-hint">Minimum time between fires (0 = no cooldown)</span>
            </div>

            <label class="checkbox-label">
              <input type="checkbox" bind:checked={enabled} />
              <span>Enable immediately</span>
            </label>
          </div>

          <div class="review-summary">
            <div class="summary-row">
              <span class="summary-label">Source</span>
              <span class="summary-value">{sourceInfo?.name.replace('_', ' ') || selectedSource}</span>
            </div>
            <div class="summary-row">
              <span class="summary-label">Action</span>
              <span class="summary-value">{actionType.replace('_', ' ')}</span>
            </div>
            {#if conditions.length > 0}
              <div class="summary-row">
                <span class="summary-label">Conditions</span>
                <span class="summary-value">{conditions.length} filter(s)</span>
              </div>
            {/if}
            {#if resolvedThreadId}
              <div class="summary-row">
                <span class="summary-label">Thread</span>
                <span class="summary-value">{resolvedThreadId.substring(0, 12)}...</span>
              </div>
            {/if}
          </div>

          {#if saveError}
            <div class="save-error">{saveError}</div>
          {/if}
        </div>
      {/if}
    </div>

    <!-- Footer -->
    <div class="wizard-footer">
      {#if stepIndex > 0 && !(isEditing && currentStep === 'configure')}
        <button class="nav-btn secondary" onclick={back} type="button">Back</button>
      {:else}
        <div></div>
      {/if}

      <div class="footer-right">
        {#if currentStep === 'conditions'}
          <button class="nav-btn secondary" onclick={next} type="button">Skip</button>
        {/if}

        {#if currentStep === 'review'}
          <button
            class="nav-btn primary"
            onclick={handleSave}
            disabled={!canAdvance() || saving}
            type="button"
          >
            {saving ? 'Saving...' : isEditing ? 'Save Changes' : 'Create Trigger'}
          </button>
        {:else}
          <button
            class="nav-btn primary"
            onclick={next}
            disabled={!canAdvance()}
            type="button"
          >
            Next
          </button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .wizard-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.15s ease-out;
  }

  .wizard-backdrop {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(4px);
  }

  .wizard-modal {
    position: relative;
    width: 560px;
    max-width: 90vw;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated);
    border-radius: var(--radius-lg);
    /* §7 — floating wizard modal: shadow alone defines elevation; the
       hairline --glass-border on a solid background was redundant
       chrome. Tokenized to --shadow-xl. */
    box-shadow: var(--shadow-xl);
    animation: slideUp 0.2s ease-out;
  }

  @keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes slideUp {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .wizard-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--glass-border);
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }

  /* Progress bar */
  .wizard-progress {
    display: flex;
    align-items: center;
    padding: var(--spacing-sm) var(--spacing-lg);
    gap: 0;
    border-bottom: 1px solid var(--glass-border);
  }

  .progress-step {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
    cursor: default;
    transition: all var(--transition-fast);
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }

  .progress-step.clickable {
    cursor: pointer;
  }

  .progress-step.clickable:hover {
    background: var(--bg-elevated-2);
  }

  .progress-step.active {
    color: var(--accent-primary);
    font-weight: 600;
  }

  .progress-step.completed {
    color: var(--success);
  }

  .step-number {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--bg-elevated-2);
    font-size: var(--font-size-3xs);
    font-weight: 600;
  }

  .progress-step.active .step-number {
    background: var(--accent-primary);
    color: var(--text-on-accent);
  }

  .progress-step.completed .step-number {
    background: var(--success);
    color: white;
  }

  .progress-line {
    flex: 1;
    height: 1px;
    background: var(--glass-border);
    min-width: 8px;
  }

  .progress-line.filled {
    background: var(--success);
  }

  /* Body */
  .wizard-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
  }

  .step-content h3 {
    margin: 0 0 var(--spacing-xs);
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
  }

  .step-desc {
    margin: 0 0 var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .optional-badge {
    font-size: var(--font-size-3xs);
    font-weight: 400;
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    padding: 1px 6px;
    border-radius: var(--radius-sm);
    vertical-align: middle;
  }

  /* Source selection grid */
  .source-filters {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-md);
  }

  .search-input {
    width: 100%;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .search-input:focus {
    border-color: var(--accent-primary);
  }

  .category-tabs {
    display: flex;
    /* §3 chip-row gap — keeps the filter pills from crowding edge-to-edge
       and matches the canonical 8px chip-row spacing. */
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .cat-tab {
    padding: 2px 10px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    text-transform: capitalize;
    transition: all var(--transition-fast);
  }

  .cat-tab:hover {
    border-color: var(--text-muted);
    color: var(--text-primary);
  }

  .cat-tab.active {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    /* Theme-aware text on accent fill: dark ink on Platinum's pale accent,
       white on Midnight/Light. Hardcoded white would vanish on Platinum. */
    color: var(--text-on-accent);
  }

  .source-grid {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .source-card {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    text-align: left;
    transition: all var(--transition-fast);
  }

  .source-card:hover {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.05);
  }

  .source-card.selected {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.1);
    box-shadow: var(--accent-glow-sm);
  }

  .source-card-icon {
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent-secondary) 15%, transparent);
    color: var(--accent-secondary, var(--accent-primary));
  }

  .source-card-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .source-card-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .source-card-desc {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .auth-badge {
    display: inline-block;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    color: var(--warning);
    background: color-mix(in srgb, var(--warning) 15%, transparent);
    padding: 1px 5px;
    border-radius: var(--radius-sm);
    letter-spacing: 0.03em;
    text-indent: 0.03em;
  }

  /* Config form */
  .setup-guide {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    background: var(--bg-elevated-2);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    border-left: 3px solid var(--accent-primary);
    margin-bottom: var(--spacing-md);
    line-height: 1.5;
    white-space: pre-line;
  }

  .config-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-title {
    margin: 0;
    font-size: var(--font-size-2xs);
    font-weight: 600;
    text-transform: uppercase;
    color: var(--text-muted);
    letter-spacing: 0.05em;
  }

  .field-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .field-label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .required {
    color: var(--error);
  }

  .field-input,
  .field-select {
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .field-input:focus,
  .field-select:focus {
    border-color: var(--accent-primary);
  }

  .field-hint {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    accent-color: var(--accent-primary);
  }

  /* Conditions */
  .no-conditions {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    font-style: italic;
  }

  .condition-row {
    display: flex;
    gap: var(--spacing-xs);
    align-items: center;
    margin-bottom: var(--spacing-xs);
  }

  .condition-field,
  .condition-op {
    padding: var(--spacing-xs);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
  }

  .condition-field {
    width: 120px;
  }

  .condition-op {
    width: 110px;
  }

  .condition-value {
    flex: 1;
    padding: var(--spacing-xs);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    outline: none;
  }

  .condition-value:focus {
    border-color: var(--accent-primary);
  }

  .condition-remove {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .condition-remove:hover {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
  }

  .add-condition-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
    margin-top: var(--spacing-xs);
  }

  .add-condition-btn:hover {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
  }

  /* Action step */
  .action-type-selector {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-md);
  }

  .action-type-btn {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    text-align: left;
    transition: all var(--transition-fast);
    color: var(--text-secondary);
  }

  .action-type-btn:hover {
    border-color: var(--accent-primary);
  }

  .action-type-btn.selected {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.08);
    color: var(--text-primary);
  }

  .at-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .at-desc {
    flex: 1;
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .action-config {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .template-textarea {
    width: 100%;
    padding: var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: var(--font-mono);
    resize: vertical;
    outline: none;
    line-height: 1.5;
    transition: border-color var(--transition-fast);
  }

  .template-textarea:focus {
    border-color: var(--accent-primary);
  }

  .template-hints {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: center;
  }

  .hints-label {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .var-hint {
    font-size: var(--font-size-3xs);
    padding: 1px 5px;
    background: var(--bg-elevated-2);
    border-radius: var(--radius-sm);
    color: var(--accent-secondary, var(--accent-primary));
    font-family: var(--font-mono);
  }

  /* Review step */
  .review-form {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-md);
  }

  .review-summary {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
  }

  .summary-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: var(--font-size-sm);
  }

  .summary-label {
    color: var(--text-muted);
    font-weight: 500;
  }

  .summary-value {
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .save-error {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  /* Footer */
  .wizard-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--glass-border);
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-xs);
  }

  .nav-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
    border: 1px solid transparent;
  }

  .nav-btn.primary {
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border-color: var(--accent-primary);
  }

  .nav-btn.primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .nav-btn.primary:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .nav-btn.secondary {
    background: transparent;
    color: var(--text-secondary);
    border-color: var(--glass-border);
  }

  .nav-btn.secondary:hover {
    color: var(--text-primary);
    border-color: var(--text-muted);
  }
</style>
