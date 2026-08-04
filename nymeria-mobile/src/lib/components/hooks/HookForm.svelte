<script lang="ts">
  import type {
    Hook,
    HookAction,
    HookCondition,
    HookCreateRequest,
    HookEvent,
    HookScope,
    HookTemplate,
    HookUpdateRequest,
  } from '$lib/types';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { trapFocus } from '$lib/actions/focus';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    DIALOG_RISE_IN,
    DIALOG_RISE_OUT,
  } from '$lib/utils/transitions';
  import { Icon } from '$lib/components/common';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    hookCategory,
    hookActionMeta,
    HOOK_ACTION_META,
    HOOK_EVENT_ACTIONS,
    HOOK_EVENT_META,
    HOOK_OPERATORS,
    FIRE_GATE_OPERATORS,
    HOOK_MATCHER_EVENTS,
  } from '$lib/utils/hooks';

  interface Props {
    threadId?: string;
    editHook?: Hook;
    onClose: () => void;
    onCreated?: (hook: Hook) => void;
  }

  let { threadId, editHook, onClose, onCreated }: Props = $props();

  const isEditing = $derived(!!editHook);

  // The reserved built-in hook (turn metadata): identity is locked
  // server-side (event/action/scope/single_use all reject on PATCH), so the
  // form drops to a restricted mode editing only what the backend accepts:
  // name, template text, fire gate, once, enabled. The action fallback covers
  // hook objects cached before `system` rode the wire.
  const isSystem = $derived(
    !!(editHook && (editHook.system || (editHook.action as string) === 'turn_metadata'))
  );

  // Mirrors the backend authoring frame (core/prompts.py
  // TURN_METADATA_TEMPLATE_PATTERN): exactly two lines, '[Time: <interior>]'
  // then '[Trigger: <interior>]', non-empty interiors with no ']' and no
  // newline. Instant feedback only; the backend re-validates on save.
  const TURN_METADATA_FRAME = /^\[Time:[^\]\n]+\]\n\[Trigger:[^\]\n]+\]$/;

  const resolvedThreadId = $derived(threadId || threadsStore.currentThreadId || '');

  type UpdateRow = { key: string; value: string };

  const EVENTS: HookEvent[] = [
    'prompt_submit',
    'pre_tool_use',
    'post_tool_use',
    'done',
    'command_submit',
  ];

  // Seed the form once from the edit target (reading props inside a function
  // keeps the state initializers warning-free); the form owns its state after.
  function initialFormState() {
    const h = editHook;
    const logic = (h?.logic ?? {}) as Record<string, unknown>;
    return {
      name: h?.name ?? '',
      event: (h?.event ?? 'prompt_submit') as HookEvent,
      action: (h?.action ?? 'inject_context') as HookAction,
      scope: (h?.scope ?? (threadId ? 'thread' : 'global')) as HookScope,
      matcher: h?.matcher ?? '',
      text: String(logic.text ?? h?.text ?? ''),
      url: String(logic.url ?? ''),
      reason: String(logic.reason ?? ''),
      command: String(logic.command ?? ''),
      timeoutSeconds:
        h?.action !== 'require_approval' && typeof logic.timeout_seconds === 'number'
          ? logic.timeout_seconds
          : 10,
      // require_approval keeps its own window so switching actions never
      // drags run_command's 10s default into a 10..600s approval hold.
      approvalWindowSeconds:
        h?.action === 'require_approval' && typeof logic.timeout_seconds === 'number'
          ? logic.timeout_seconds
          : 180,
      prompt: String(logic.prompt ?? ''),
      workflowId: String(logic.workflow_id ?? ''),
      workflowParams:
        h?.action === 'run_workflow' && logic.params && typeof logic.params === 'object'
          && Object.keys(logic.params as Record<string, unknown>).length > 0
          ? JSON.stringify(logic.params, null, 2)
          : '',
      onFault: (logic.on_fault === 'deny' ? 'deny' : 'allow') as 'allow' | 'deny',
      // run_workflow keeps its own wall clock so switching actions never drags
      // run_command's 10s default into a 5..600s workflow budget.
      workflowTimeoutSeconds:
        h?.action === 'run_workflow' && typeof logic.timeout_seconds === 'number'
          ? logic.timeout_seconds
          : 60,
      conditions: Array.isArray(logic.conditions)
        ? (logic.conditions as HookCondition[]).map((c) => ({ ...c }))
        : [],
      updateRows:
        logic.updates && typeof logic.updates === 'object'
          ? Object.entries(logic.updates as Record<string, string>).map(([key, value]) => ({
              key,
              value: String(value),
            }))
          : [],
      // Fire gate + lifecycle (top-level on the hook, not inside `logic`).
      fireConditions: Array.isArray(h?.fire_conditions)
        ? (h.fire_conditions as HookCondition[]).map((c) => ({ ...c }))
        : [],
      once: h?.once ?? false,
      singleUse: h?.single_use ?? false,
      enabled: h?.enabled ?? true,
    };
  }
  const init = initialFormState();

  let name = $state(init.name);
  let event = $state<HookEvent>(init.event);
  let action = $state<HookAction>(init.action);
  let scope = $state<HookScope>(init.scope);
  let matcher = $state(init.matcher);
  let text = $state(init.text);
  let url = $state(init.url);
  let reason = $state(init.reason);
  let command = $state(init.command);
  let timeoutSeconds = $state(init.timeoutSeconds);
  let approvalWindowSeconds = $state(init.approvalWindowSeconds);
  let approvalPrompt = $state(init.prompt);
  let workflowId = $state(init.workflowId);
  let workflowParams = $state(init.workflowParams);
  let onFault = $state<'allow' | 'deny'>(init.onFault);
  let workflowTimeoutSeconds = $state(init.workflowTimeoutSeconds);
  let conditions = $state<HookCondition[]>(init.conditions);
  let updateRows = $state<UpdateRow[]>(init.updateRows);
  let fireConditions = $state<HookCondition[]>(init.fireConditions);
  let once = $state(init.once);
  let singleUse = $state(init.singleUse);
  let enabled = $state(init.enabled);

  let saving = $state(false);
  let saveError = $state<string | null>(null);

  // Bundled-template strip (create mode only). The catalog is a bonus: a
  // fetch failure just hides the strip, it never blocks authoring.
  let templates = $state<HookTemplate[]>([]);
  let installingId = $state<string | null>(null);
  let installNote = $state<string | null>(null);

  onMount(() => {
    if (isEditing) return;
    hooksStore
      .getTemplates()
      .then((list) => (templates = list))
      .catch(() => (templates = []));
  });

  /**
   * Install a bundled template with its own default binding. A fresh install
   * closes the form (the hook appears in the feed); an idempotent re-install
   * keeps the form open with a note.
   */
  async function handleInstallTemplate(template: HookTemplate) {
    installingId = template.id;
    installNote = null;
    saveError = null;
    try {
      const result = await hooksStore.installTemplate(template.id);
      if (result.created) {
        onCreated?.(result.hook);
        onClose();
      } else {
        installNote = `"${template.title}" is already installed.`;
      }
    } catch (e) {
      saveError = humanizeErrorText(e, { action: 'install', resource: 'the template' });
    } finally {
      installingId = null;
    }
  }

  // ---- derived legality --------------------------------------------------
  const legalActions = $derived(HOOK_EVENT_ACTIONS[event] ?? []);
  const category = $derived(hookCategory(action));
  const eventMeta = $derived(HOOK_EVENT_META[event]);
  // Accessor form, NOT the raw table: the system turn-metadata action sits
  // outside the authorable union, and a raw table miss here crashed the app
  // shell on `undefined.hint` when editing the built-in hook.
  const actionMeta = $derived(hookActionMeta(action));
  const showMatcher = $derived(HOOK_MATCHER_EVENTS.includes(event));
  // command_submit matches command paths; the tool events match tool names.
  const matcherIsCommand = $derived(event === 'command_submit');
  const isTextAction = $derived(
    action === 'inject_context' || action === 'notify' || action === 'create_todo'
  );

  const textLabel = $derived(
    action === 'notify'
      ? 'Notification text'
      : action === 'create_todo'
        ? 'Task text'
        : action === 'webhook'
          ? 'Request body'
          : 'Context to inject'
  );

  // When the event changes, keep the action legal for it.
  function onEventChange(next: HookEvent) {
    event = next;
    const legal = HOOK_EVENT_ACTIONS[next] ?? [];
    if (!legal.includes(action)) {
      action = legal[0];
    }
  }

  function addCondition() {
    conditions = [...conditions, { field: '', operator: 'contains', value: '' }];
  }
  function removeCondition(i: number) {
    conditions = conditions.filter((_, idx) => idx !== i);
  }
  function updateCondition(i: number, patch: Partial<HookCondition>) {
    conditions = conditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c));
  }

  // Fire-gate condition rows (mirror the logic-condition helpers above).
  function addFireCondition() {
    fireConditions = [...fireConditions, { field: '', operator: 'contains', value: '' }];
  }
  function removeFireCondition(i: number) {
    fireConditions = fireConditions.filter((_, idx) => idx !== i);
  }
  function updateFireCondition(i: number, patch: Partial<HookCondition>) {
    fireConditions = fireConditions.map((c, idx) => (idx === i ? { ...c, ...patch } : c));
  }

  function addUpdateRow() {
    updateRows = [...updateRows, { key: '', value: '' }];
  }
  function removeUpdateRow(i: number) {
    updateRows = updateRows.filter((_, idx) => idx !== i);
  }

  function validate(): string | null {
    if (!name.trim()) return 'Give the hook a name before saving.';
    if (isSystem) {
      if (!TURN_METADATA_FRAME.test(text.trim())) {
        return 'The template must be exactly two lines, [Time: ...] then [Trigger: ...], with no ] or line breaks inside the brackets.';
      }
      return null;
    }
    if (!isEditing && scope === 'thread' && !resolvedThreadId) {
      return 'A thread-scoped hook needs an open thread. Switch to a thread, or set the scope to Global.';
    }
    if (isTextAction && !text.trim()) return `${textLabel} needs a value.`;
    if (action === 'webhook' && !url.trim()) return 'Webhook needs a target URL.';
    if (action === 'block_if_matches') {
      if (conditions.some((c) => !c.field.trim())) return 'Every condition needs a field name.';
    }
    if (action === 'rewrite_arg') {
      const rows = updateRows.filter((r) => r.key.trim());
      if (rows.length === 0) return 'Rewrite needs at least one argument to set.';
    }
    if (action === 'run_command') {
      if (!command.trim()) return 'A command hook needs a command to run.';
      if (!(timeoutSeconds >= 1 && timeoutSeconds <= 300))
        return 'Timeout must be between 1 and 300 seconds.';
    }
    if (action === 'require_approval') {
      if (!(approvalWindowSeconds >= 10 && approvalWindowSeconds <= 600))
        return 'Approval window must be between 10 and 600 seconds.';
    }
    if (action === 'run_workflow') {
      if (!workflowId.trim()) return 'A workflow hook needs a workflow id.';
      if (!(workflowTimeoutSeconds >= 5 && workflowTimeoutSeconds <= 600))
        return 'Workflow timeout must be between 5 and 600 seconds.';
      if (parseWorkflowParams() === null)
        return 'Workflow parameters must be a JSON object, e.g. {"key": "value"}.';
    }
    return null;
  }

  /** Parse the params textarea: {} when blank, null when invalid. */
  function parseWorkflowParams(): Record<string, unknown> | null {
    const raw = workflowParams.trim();
    if (!raw) return {};
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
      return null;
    } catch {
      return null;
    }
  }

  function buildLogicFields() {
    const cleanConditions = conditions
      .filter((c) => c.field.trim())
      .map((c) => ({ ...c, field: c.field.trim() }));
    const updates: Record<string, string> = {};
    for (const row of updateRows) {
      if (row.key.trim()) updates[row.key.trim()] = row.value;
    }
    return { cleanConditions, updates };
  }

  /** Drop incomplete fire-gate rows (the gate is optional; no hard error). */
  function buildFireConditions(): HookCondition[] {
    return fireConditions
      .filter((c) => c.field.trim())
      .map((c) => ({ ...c, field: c.field.trim() }));
  }

  async function handleSave() {
    const err = validate();
    if (err) { saveError = err; return; }
    saving = true;
    saveError = null;

    const { cleanConditions, updates } = buildLogicFields();
    const fireConditionsClean = buildFireConditions();
    const trimmedMatcher = matcher.trim();

    try {
      if (isSystem && editHook) {
        // System hooks accept ONLY these fields; event/action/scope/
        // single_use are locked server-side and any of them would 400 the
        // whole PATCH. Template is trimmed so a trailing newline cannot fail
        // the backend's exact-frame validator.
        const req: HookUpdateRequest = {
          name: name.trim(),
          enabled,
          text: text.trim(),
          fire_conditions: fireConditionsClean,
          once,
        };
        await hooksStore.updateHook(editHook.id, req);
      } else if (isEditing && editHook) {
        const req: HookUpdateRequest = {
          name: name.trim(),
          event,
          action,
          enabled,
          // Send "" (not null) to clear: the backend PATCH drops null scalars
          // (`exclude_none`), but an empty-string matcher is normalized to None
          // (any tool) by HookDefinition's validator. null would silently keep
          // the old, narrower matcher.
          matcher: showMatcher ? (trimmedMatcher || '') : '',
          // Fire gate + lifecycle apply to every event/action. Always send them
          // on update so clearing the rows / toggling off actually persists
          // (empty list clears fire_conditions; the bools apply as-is).
          fire_conditions: fireConditionsClean,
          once,
          single_use: singleUse,
        };
        if (isTextAction || action === 'webhook') req.text = text;
        if (action === 'webhook') req.url = url.trim();
        if (action === 'block_if_matches') {
          req.conditions = cleanConditions;
          // "" (not undefined) so an emptied reason actually clears back to the
          // default deny message; undefined would merge and keep the old reason.
          req.reason = reason.trim();
        }
        if (action === 'rewrite_arg') {
          req.conditions = cleanConditions;
          req.updates = updates;
        }
        if (action === 'run_command') {
          req.command = command.trim();
          req.timeout_seconds = timeoutSeconds;
        }
        if (action === 'require_approval') {
          // "" clears back to the default prompt (the backend aliases the
          // flat text field to the prompt param for this action).
          req.text = approvalPrompt.trim();
          req.conditions = cleanConditions;
          req.timeout_seconds = approvalWindowSeconds;
        }
        if (action === 'run_workflow') {
          req.workflow_id = workflowId.trim();
          // {} (not undefined) so emptying the field actually clears the
          // stored params; undefined would merge and keep the old binding.
          req.workflow_params = parseWorkflowParams() ?? {};
          req.on_fault = onFault;
          req.timeout_seconds = workflowTimeoutSeconds;
        }
        await hooksStore.updateHook(editHook.id, req);
      } else {
        const req: HookCreateRequest = {
          name: name.trim(),
          event,
          action,
          scope,
          enabled,
          thread_id: scope === 'thread' ? resolvedThreadId : undefined,
          matcher: showMatcher && trimmedMatcher ? trimmedMatcher : undefined,
          once,
          single_use: singleUse,
        };
        if (fireConditionsClean.length) req.fire_conditions = fireConditionsClean;
        if (isTextAction || action === 'webhook') req.text = text;
        if (action === 'webhook') req.url = url.trim();
        if (action === 'block_if_matches') {
          req.conditions = cleanConditions;
          req.reason = reason.trim() || undefined;
        }
        if (action === 'rewrite_arg') {
          req.conditions = cleanConditions;
          req.updates = updates;
        }
        if (action === 'run_command') {
          req.command = command.trim();
          req.timeout_seconds = timeoutSeconds;
        }
        if (action === 'require_approval') {
          if (approvalPrompt.trim()) req.text = approvalPrompt.trim();
          req.conditions = cleanConditions;
          req.timeout_seconds = approvalWindowSeconds;
        }
        if (action === 'run_workflow') {
          req.workflow_id = workflowId.trim();
          const parsedParams = parseWorkflowParams();
          if (parsedParams && Object.keys(parsedParams).length > 0) {
            req.workflow_params = parsedParams;
          }
          req.on_fault = onFault;
          req.timeout_seconds = workflowTimeoutSeconds;
        }
        const created = await hooksStore.createHook(req);
        onCreated?.(created);
      }
      onClose();
    } catch (e) {
      saveError = humanizeErrorText(e, { action: 'save', resource: 'the hook' });
    } finally {
      saving = false;
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="form-overlay" in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
  <button
    class="form-backdrop"
    type="button"
    tabindex="-1"
    aria-label={isEditing ? 'Close edit hook dialog' : 'Close new hook dialog'}
    onclick={onClose}
  ></button>
  <div
    class="form-modal cat-{category}"
    role="dialog"
    aria-modal="true"
    aria-labelledby="hook-form-title"
    tabindex="-1"
    use:trapFocus
    in:fly={DIALOG_RISE_IN}
    out:fly={DIALOG_RISE_OUT}
  >
    <div class="form-header">
      <h2 id="hook-form-title">{isEditing ? (isSystem ? 'Edit Built-in Hook' : 'Edit Hook') : 'New Hook'}</h2>
      <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
        <Icon name="x" size={16} />
      </button>
    </div>

    <div class="form-body">
      {#if !isEditing && templates.length > 0}
        <!-- Bundled templates: one-click ready-made hooks (idempotent). -->
        <div class="template-strip">
          <span class="template-heading">Start from a template</span>
          {#each templates as template (template.id)}
            <div class="template-row">
              <div class="template-info">
                <span class="template-title">{template.title}</span>
                <span class="template-desc">{template.description}</span>
              </div>
              <button
                class="template-install"
                type="button"
                disabled={installingId !== null || saving}
                onclick={() => handleInstallTemplate(template)}
              >
                {installingId === template.id ? 'Installing…' : 'Install'}
              </button>
            </div>
          {/each}
          {#if installNote}
            <span class="template-note">{installNote}</span>
          {/if}
          <span class="template-divider">or build your own</span>
        </div>
      {/if}

      <!-- Name -->
      <div class="field-row">
        <label class="field-label" for="hook-name">Name <span class="required">*</span></label>
        <input
          id="hook-name"
          class="field-input"
          type="text"
          placeholder="e.g. Guard rm -rf, Inject house style"
          bind:value={name}
          maxlength={200}
        />
      </div>

      {#if isSystem}
        <!-- Locked identity: the backend rejects event/action/scope changes
             on system hooks, so no pickers, just what this hook is. -->
        <div class="system-note">
          <Icon name="clock" size={13} />
          <p>
            Built-in hook. It stamps the time and trigger at the top of every
            turn, on every thread. When it runs is fixed; the template, fire
            gate, and name are yours to change. Deleting it resets these
            defaults.
          </p>
        </div>
        <div class="field-row">
          <label class="field-label" for="hook-template">
            Metadata template <span class="required">*</span>
          </label>
          <textarea
            id="hook-template"
            class="field-textarea"
            bind:value={text}
            rows={2}
            maxlength={300}
          ></textarea>
          <span class="field-hint">
            Exactly two lines, <code>[Time: ...]</code> then
            <code>[Trigger: ...]</code>. Customize the text inside the
            brackets; <code>{'{time}'}</code> and <code>{'{trigger}'}</code>
            are filled in each turn. No <code>]</code> or line breaks inside.
          </span>
        </div>
      {:else}
        <!-- When (event) -->
        <div class="field-row">
          <label class="field-label" for="hook-event">When</label>
          <select
            id="hook-event"
            class="field-select"
            value={event}
            onchange={(e) => onEventChange((e.target as HTMLSelectElement).value as HookEvent)}
          >
            {#each EVENTS as ev}
              <option value={ev}>{HOOK_EVENT_META[ev].label}</option>
            {/each}
          </select>
          <span class="field-hint">{eventMeta.hint}</span>
        </div>

        <!-- Do (action) -->
        <div class="field-row">
          <span class="field-label">Do</span>
          <div class="action-picker">
            {#each legalActions as act}
              {@const meta = HOOK_ACTION_META[act]}
              <button
                class="action-opt cat-{hookCategory(act)}"
                class:selected={action === act}
                type="button"
                onclick={() => (action = act)}
              >
                <Icon name={meta.icon} size={15} />
                <span class="opt-name">{meta.label}</span>
              </button>
            {/each}
          </div>
          <span class="field-hint">{actionMeta.hint}</span>
        </div>
      {/if}

      <!-- Scope (create only; re-scoping is a delete + create) -->
      {#if !isEditing}
        <div class="field-row">
          <span class="field-label">Scope</span>
          <div class="segmented-toggle" role="group" aria-label="Hook scope">
            <button
              class="seg" class:active={scope === 'thread'}
              type="button" onclick={() => (scope = 'thread')}
              disabled={!resolvedThreadId}
            >This thread</button>
            <button
              class="seg" class:active={scope === 'global'}
              type="button" onclick={() => (scope = 'global')}
            >Global</button>
          </div>
          <span class="field-hint">
            {scope === 'thread'
              ? 'Runs only on the current thread.'
              : 'Runs on every thread.'}
          </span>
        </div>
      {/if}

      <!-- Name matcher (tool events: tool names; command_submit: command paths) -->
      {#if showMatcher}
        <div class="field-row">
          <label class="field-label" for="hook-matcher">
            {matcherIsCommand ? 'Command matcher' : 'Tool matcher'}
          </label>
          <input
            id="hook-matcher"
            class="field-input"
            type="text"
            placeholder={matcherIsCommand
              ? 'e.g. clear, provider *  (blank = any command)'
              : 'e.g. bash, Edit|Write  (blank = any tool)'}
            bind:value={matcher}
          />
          <span class="field-hint">
            {matcherIsCommand
              ? 'Only run for matching command paths; a trailing * matches a family. Leave blank for all commands.'
              : 'Only run for tools whose name matches. Leave blank for all tools.'}
          </span>
        </div>
      {/if}

      <!-- Action-specific params -->
      {#if isTextAction || action === 'webhook'}
        {#if action === 'webhook'}
          <div class="field-row">
            <label class="field-label" for="hook-url">Webhook URL <span class="required">*</span></label>
            <input
              id="hook-url"
              class="field-input"
              type="text"
              placeholder="https://example.com/hook"
              bind:value={url}
            />
          </div>
        {/if}
        <div class="field-row">
          <label class="field-label" for="hook-text">
            {textLabel}
            {#if isTextAction}<span class="required">*</span>{/if}
          </label>
          <textarea
            id="hook-text"
            class="field-textarea"
            bind:value={text}
            rows={4}
            maxlength={10000}
            placeholder="Supports {'{placeholders}'} like {'{tool_name}'}, {'{final_text}'}, {'{prompt}'}"
          ></textarea>
          <span class="field-hint">
            Placeholders are filled at fire time (e.g. <code>{'{tool_name}'}</code>, <code>{'{prompt}'}</code>).
          </span>
        </div>
      {/if}

      {#if action === 'block_if_matches' || action === 'rewrite_arg' || action === 'require_approval'}
        <div class="field-row">
          <span class="field-label">
            Conditions
            <span class="optional-badge">{action === 'rewrite_arg' ? 'optional' : 'blank = always'}</span>
          </span>
          {#each conditions as condition, i (i)}
            <div class="condition-row">
              <input
                class="condition-field"
                type="text"
                placeholder="arg (e.g. command)"
                value={condition.field}
                oninput={(e) => updateCondition(i, { field: (e.target as HTMLInputElement).value })}
              />
              <select
                class="condition-op"
                value={condition.operator}
                onchange={(e) => updateCondition(i, { operator: (e.target as HTMLSelectElement).value as HookCondition['operator'] })}
              >
                {#each HOOK_OPERATORS as op}
                  <option value={op.value}>{op.label}</option>
                {/each}
              </select>
              <input
                class="condition-value"
                type="text"
                placeholder="value"
                value={condition.value}
                oninput={(e) => updateCondition(i, { value: (e.target as HTMLInputElement).value })}
              />
              <button class="row-remove" onclick={() => removeCondition(i)} type="button" aria-label="Remove condition">
                <Icon name="x" size={12} />
              </button>
            </div>
          {/each}
          <button class="add-row-btn" onclick={addCondition} type="button">
            <Icon name="plus" size={12} /> <span>Add condition</span>
          </button>
        </div>
      {/if}

      {#if action === 'block_if_matches'}
        <div class="field-row">
          <label class="field-label" for="hook-reason">Denial reason</label>
          <input
            id="hook-reason"
            class="field-input"
            type="text"
            placeholder="Shown to the agent when blocked"
            bind:value={reason}
            maxlength={500}
          />
        </div>
      {/if}

      {#if action === 'require_approval'}
        <div class="field-row">
          <label class="field-label" for="hook-approval-prompt">Approval prompt</label>
          <input
            id="hook-approval-prompt"
            class="field-input"
            type="text"
            placeholder={'Blank = "Approve tool call {tool_name}?"'}
            bind:value={approvalPrompt}
            maxlength={500}
          />
          <span class="field-hint">
            Shown with the Approve/Deny buttons. Placeholders like <code>{'{tool_name}'}</code> are filled at fire time.
          </span>
        </div>
        <div class="field-row">
          <label class="field-label" for="hook-approval-window">Approval window (seconds)</label>
          <input
            id="hook-approval-window"
            class="field-input"
            type="number"
            min={10}
            max={600}
            bind:value={approvalWindowSeconds}
          />
          <span class="field-hint">
            10 to 600. The tool call waits this long for your decision; no answer denies it.
          </span>
        </div>
      {/if}

      {#if action === 'rewrite_arg'}
        <div class="field-row">
          <span class="field-label">Set arguments <span class="required">*</span></span>
          {#each updateRows as row, i (i)}
            <div class="condition-row">
              <input
                class="condition-field"
                type="text"
                placeholder="arg name"
                bind:value={row.key}
              />
              <span class="set-eq">=</span>
              <input
                class="condition-value"
                type="text"
                placeholder="new value"
                bind:value={row.value}
              />
              <button class="row-remove" onclick={() => removeUpdateRow(i)} type="button" aria-label="Remove argument">
                <Icon name="x" size={12} />
              </button>
            </div>
          {/each}
          <button class="add-row-btn" onclick={addUpdateRow} type="button">
            <Icon name="plus" size={12} /> <span>Add argument</span>
          </button>
        </div>
      {/if}

      {#if action === 'run_workflow'}
        <div class="field-row">
          <label class="field-label" for="hook-workflow-id">Workflow <span class="required">*</span></label>
          <input
            id="hook-workflow-id"
            class="field-input"
            type="text"
            placeholder="Published workflow tool id, e.g. my_workflow"
            bind:value={workflowId}
            maxlength={200}
          />
          <span class="field-hint">
            Must be a published, approved workflow. The hook context is passed as its
            <code>event</code> parameter when the workflow declares one.
          </span>
        </div>
        <div class="field-row">
          <label class="field-label" for="hook-workflow-params">
            Parameters <span class="optional-badge">optional</span>
          </label>
          <textarea
            id="hook-workflow-params"
            class="field-textarea"
            bind:value={workflowParams}
            rows={3}
            placeholder={'JSON object of static params, e.g. {"channel": "alerts"}'}
          ></textarea>
        </div>
        <div class="field-row">
          <label class="field-label" for="hook-workflow-timeout">Timeout (seconds)</label>
          <input
            id="hook-workflow-timeout"
            class="field-input"
            type="number"
            min={5}
            max={600}
            bind:value={workflowTimeoutSeconds}
          />
          <span class="field-hint">5 to 600. The workflow run is killed at this wall clock.</span>
        </div>
        {#if event === 'pre_tool_use'}
          <div class="field-row">
            <span class="field-label">If the workflow fails</span>
            <div class="segmented-toggle" role="group" aria-label="Workflow fault policy">
              <button
                class="seg" class:active={onFault === 'allow'}
                type="button" onclick={() => (onFault = 'allow')}
              >Allow the tool call</button>
              <button
                class="seg" class:active={onFault === 'deny'}
                type="button" onclick={() => (onFault = 'deny')}
              >Deny the tool call</button>
            </div>
            <span class="field-hint">
              {onFault === 'allow'
                ? 'A faulting workflow lets the tool call proceed, with a note in the hook log.'
                : 'A faulting workflow blocks the tool call (fail closed).'}
            </span>
          </div>
        {/if}
      {/if}

      {#if action === 'run_command'}
        <div class="field-row">
          <label class="field-label" for="hook-command">Command <span class="required">*</span></label>
          <textarea
            id="hook-command"
            class="field-textarea"
            bind:value={command}
            rows={3}
            maxlength={4000}
            placeholder="Shell command. The hook context arrives as JSON on stdin."
          ></textarea>
          <span class="field-hint">
            Runs on the server as the backend user (admin only; must be enabled on the server).
            On <code>pre_tool_use</code>, exit 2 denies the tool call (stderr is the reason).
          </span>
        </div>
        <div class="field-row">
          <label class="field-label" for="hook-timeout">Timeout (seconds)</label>
          <input
            id="hook-timeout"
            class="field-input"
            type="number"
            min={1}
            max={300}
            bind:value={timeoutSeconds}
          />
          <span class="field-hint">1 to 300. In-band events (on your message / before a tool) are capped at 60.</span>
        </div>
      {/if}

      <!-- Fire gate (applies to every event/action; distinct from the
           action-specific guardrail conditions above) -->
      <div class="field-row">
        <span class="field-label">
          Fire gate
          <span class="optional-badge">optional</span>
        </span>
        <span class="field-hint">
          Only fire when all of these match: meta fields like <code>event</code>,
          <code>tool_name</code>, <code>prompt</code>; tool args as
          <code>args.name</code>; context numbers like
          <code>context_pct_of_trigger</code> (with the numeric operators). Blank = always fire.
        </span>
        {#each fireConditions as condition, i (i)}
          <div class="condition-row">
            <input
              class="condition-field"
              type="text"
              placeholder="field (e.g. tool_name)"
              value={condition.field}
              oninput={(e) => updateFireCondition(i, { field: (e.target as HTMLInputElement).value })}
            />
            <select
              class="condition-op"
              value={condition.operator}
              onchange={(e) => updateFireCondition(i, { operator: (e.target as HTMLSelectElement).value as HookCondition['operator'] })}
            >
              {#each FIRE_GATE_OPERATORS as op}
                <option value={op.value}>{op.label}</option>
              {/each}
            </select>
            <input
              class="condition-value"
              type="text"
              placeholder="value"
              value={condition.value}
              oninput={(e) => updateFireCondition(i, { value: (e.target as HTMLInputElement).value })}
            />
            <button class="row-remove" onclick={() => removeFireCondition(i)} type="button" aria-label="Remove fire condition">
              <Icon name="x" size={12} />
            </button>
          </div>
        {/each}
        <button class="add-row-btn" onclick={addFireCondition} type="button">
          <Icon name="plus" size={12} /> <span>Add fire condition</span>
        </button>
      </div>

      <label class="checkbox-label">
        <input type="checkbox" bind:checked={once} />
        <span>Fire once, re-arm when the gate stops matching</span>
      </label>

      <label class="checkbox-label">
        <input type="checkbox" bind:checked={enabled} />
        <span>Enabled</span>
      </label>

      {#if !isSystem}
        <label class="checkbox-label">
          <input type="checkbox" bind:checked={singleUse} />
          <span>Delete after its first successful run (single-use)</span>
        </label>
      {/if}

      {#if saveError}
        <div class="save-error">{saveError}</div>
      {/if}
    </div>

    <div class="form-footer">
      <button class="nav-btn secondary" onclick={onClose} type="button" disabled={saving}>Cancel</button>
      <button
        class="nav-btn primary"
        onclick={handleSave}
        type="button"
        disabled={saving || installingId !== null}
      >
        {saving ? 'Saving…' : isEditing ? 'Save Changes' : 'Create Hook'}
      </button>
    </div>
  </div>
</div>

<style>
  .form-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .form-backdrop {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(4px);
  }

  .form-modal {
    --cat-color: var(--accent-primary);
    position: relative;
    width: 560px;
    max-width: 90vw;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    background: var(--bg-elevated);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-xl);
  }

  .form-modal.cat-guardrails { --cat-color: var(--warning); }
  .form-modal.cat-context { --cat-color: var(--accent-primary); }
  .form-modal.cat-reactions { --cat-color: var(--info, var(--accent-secondary, var(--text-secondary))); }
  .form-modal.cat-commands { --cat-color: var(--accent-secondary, var(--text-secondary)); }

  .form-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--glass-border);
  }

  .form-header h2 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
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

  .form-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
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

  .required { color: var(--error); }

  .optional-badge {
    font-size: var(--font-size-3xs);
    font-weight: 400;
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    padding: 1px 6px;
    border-radius: var(--radius-sm);
    margin-left: var(--spacing-xs);
    vertical-align: middle;
  }

  .field-hint {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    max-width: 60ch;
    line-height: 1.45;
  }

  .field-hint code {
    font-family: var(--font-mono);
    font-size: calc(var(--font-size-2xs) - 0.5px);
    color: var(--text-secondary);
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

  .field-textarea {
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

  .field-textarea:focus {
    border-color: var(--accent-primary);
  }

  /* Bundled-template strip (create mode): flat rows under one heading, set
     apart from the authoring fields by a dashed divider line. */
  .template-strip {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .template-heading {
    font-size: var(--font-size-3xs);
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .template-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    min-width: 0;
  }

  .template-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .template-title {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .template-desc {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .template-install {
    flex-shrink: 0;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .template-install:hover:not(:disabled) {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
  }

  .template-install:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .template-note {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
  }

  .template-divider {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .template-divider::before,
  .template-divider::after {
    content: '';
    flex: 1;
    border-top: 1px dashed var(--border-subtle, var(--border-default));
  }

  /* Locked-identity note for the built-in system hook: left-accent info
     strip, deliberately lighter than a full card (design guide: vary
     container treatments). */
  .system-note {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-sm-plus);
    border-left: 2px solid var(--cat-color);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--cat-color) 6%, transparent);
    color: var(--text-secondary);
    font-size: var(--font-size-2xs);
    line-height: 1.45;
  }

  .system-note :global(svg) {
    flex-shrink: 0;
    margin-top: 1px;
    color: var(--cat-color);
  }

  .system-note p { margin: 0; }

  /* Action picker: category-accented option chips */
  .action-picker {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-sm);
  }

  .action-opt {
    --cat-color: var(--accent-primary);
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .action-opt.cat-guardrails { --cat-color: var(--warning); }
  .action-opt.cat-context { --cat-color: var(--accent-primary); }
  .action-opt.cat-reactions { --cat-color: var(--info, var(--accent-secondary, var(--text-secondary))); }
  .action-opt.cat-commands { --cat-color: var(--accent-secondary, var(--text-secondary)); }

  .action-opt :global(svg) { color: var(--cat-color); }

  .action-opt:hover {
    border-color: var(--cat-color);
    color: var(--text-primary);
  }

  .action-opt.selected {
    border-color: var(--cat-color);
    background: color-mix(in srgb, var(--cat-color) 12%, transparent);
    color: var(--text-primary);
  }

  /* Scope segmented control */
  .segmented-toggle {
    display: inline-flex;
    gap: 2px;
    padding: 2px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    align-self: flex-start;
  }

  .seg {
    padding: 4px 12px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .seg:disabled { opacity: 0.4; cursor: not-allowed; }

  .seg.active {
    background: var(--accent-primary);
    color: var(--text-on-accent);
  }

  /* Condition / set rows */
  .condition-row {
    display: flex;
    gap: var(--spacing-xs);
    align-items: center;
  }

  .condition-field,
  .condition-op,
  .condition-value {
    padding: var(--spacing-xs);
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    outline: none;
  }

  .condition-field { width: 130px; flex-shrink: 0; }
  .condition-op { width: 110px; flex-shrink: 0; }
  .condition-value { flex: 1; min-width: 0; }

  .condition-field:focus,
  .condition-op:focus,
  .condition-value:focus {
    border-color: var(--accent-primary);
  }

  .set-eq {
    color: var(--text-muted);
    font-weight: 600;
  }

  .row-remove {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    flex-shrink: 0;
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .row-remove:hover {
    color: var(--error);
    background: color-mix(in srgb, var(--error) 10%, transparent);
  }

  .add-row-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    align-self: flex-start;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: transparent;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-2xs);
    cursor: pointer;
    transition: all var(--transition-fast);
    margin-top: var(--spacing-2xs);
  }

  .add-row-btn:hover {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .checkbox-label input[type='checkbox'] {
    accent-color: var(--accent-primary);
  }

  .save-error {
    padding: var(--spacing-xs) var(--spacing-sm);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    border: 1px solid var(--error);
    border-radius: var(--radius-md);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  .form-footer {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--glass-border);
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

  .nav-btn.primary:hover:not(:disabled) { filter: brightness(1.1); }
  .nav-btn.primary:disabled { opacity: 0.4; cursor: not-allowed; }

  .nav-btn.secondary {
    background: transparent;
    color: var(--text-secondary);
    border-color: var(--glass-border);
  }

  .nav-btn.secondary:hover:not(:disabled) {
    color: var(--text-primary);
    border-color: var(--text-muted);
  }
</style>
