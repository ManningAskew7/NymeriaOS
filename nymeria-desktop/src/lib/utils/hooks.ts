/**
 * Lifecycle-hook taxonomy: the event/action legality map plus the
 * category-coded families (Guardrails / Context / Reactions) that drive the
 * authoring form and the feed grouping. Mirrors the backend
 * (`core/hook_manager.py` EVENT_ACTIONS + action families). Pure data + pure
 * functions so both apps keep a byte-identical copy.
 */

import type { Hook, HookAction, HookCondition, HookEvent } from '$lib/types';

export type HookCategory = 'guardrails' | 'context' | 'reactions' | 'commands';

/** Which actions are legal for each event (gates the authoring action list). */
export const HOOK_EVENT_ACTIONS: Record<HookEvent, HookAction[]> = {
  prompt_submit: ['inject_context', 'run_command', 'run_workflow'],
  pre_tool_use: [
    'block_if_matches',
    'rewrite_arg',
    'require_approval',
    'run_command',
    'run_workflow',
  ],
  post_tool_use: [
    'inject_context',
    'notify',
    'create_todo',
    'webhook',
    'run_command',
    'run_workflow',
  ],
  done: ['inject_context', 'notify', 'create_todo', 'webhook', 'run_command', 'run_workflow'],
  command_submit: [
    'block_if_matches',
    'rewrite_arg',
    'require_approval',
    'notify',
    'create_todo',
    'webhook',
    'run_command',
    'run_workflow',
  ],
};

/** Events that fire around a tool call, where a tool-name matcher applies. */
export const HOOK_TOOL_EVENTS: HookEvent[] = ['pre_tool_use', 'post_tool_use'];

/** Events that fire around a slash-command dispatch: the matcher targets
 * canonical command paths ('tools list', 'provider *') instead. */
export const HOOK_COMMAND_EVENTS: HookEvent[] = ['command_submit'];

/** All events on which the name matcher applies (the authoring form's gate). */
export const HOOK_MATCHER_EVENTS: HookEvent[] = [...HOOK_TOOL_EVENTS, ...HOOK_COMMAND_EVENTS];

const CATEGORY_OF: Record<HookAction, HookCategory> = {
  block_if_matches: 'guardrails',
  rewrite_arg: 'guardrails',
  require_approval: 'guardrails',
  inject_context: 'context',
  notify: 'reactions',
  create_todo: 'reactions',
  webhook: 'reactions',
  run_command: 'commands',
  run_workflow: 'commands',
};

export function hookCategory(action: HookAction | string): HookCategory {
  // The reserved turn-metadata system hook (action outside the authorable
  // union) injects the built-in [Time:]/[Trigger:] block: a context hook.
  if (action === 'turn_metadata') return 'context';
  return CATEGORY_OF[action as HookAction] ?? 'reactions';
}

export interface HookCategoryMeta {
  key: HookCategory;
  label: string;
  icon: string;
  description: string;
}

/** Ordered for display: guard first (most consequential), then context, reactions. */
export const HOOK_CATEGORIES: HookCategoryMeta[] = [
  {
    key: 'guardrails',
    label: 'Guardrails',
    icon: 'warning',
    description: 'Block or rewrite tool calls before they run',
  },
  {
    key: 'context',
    label: 'Context',
    icon: 'fileText',
    description: 'Add text into the conversation',
  },
  {
    key: 'reactions',
    label: 'Reactions',
    icon: 'bell',
    description: 'Notify, create a task, or call a webhook',
  },
  {
    key: 'commands',
    label: 'Custom logic',
    icon: 'terminal',
    description: 'Run a shell command (admin only) or a nym workflow',
  },
];

export interface HookActionMeta {
  label: string;
  icon: string;
  /** Short helper shown under the action picker. */
  hint: string;
}

export const HOOK_ACTION_META: Record<HookAction, HookActionMeta> = {
  inject_context: {
    label: 'Inject context',
    icon: 'fileText',
    hint: 'Add text (with {placeholders}) into the conversation.',
  },
  block_if_matches: {
    label: 'Block if matches',
    icon: 'warning',
    hint: 'Deny the tool call when the conditions match.',
  },
  rewrite_arg: {
    label: 'Rewrite argument',
    icon: 'edit',
    hint: "Change a tool argument before the call runs.",
  },
  require_approval: {
    label: 'Require approval',
    icon: 'flag',
    hint: 'Hold the tool call until you approve or deny it (no answer = deny).',
  },
  notify: {
    label: 'Notify',
    icon: 'bell',
    hint: 'Send a notification.',
  },
  create_todo: {
    label: 'Create task',
    icon: 'check',
    hint: 'Add a task to your list.',
  },
  webhook: {
    label: 'Webhook',
    icon: 'globe',
    hint: 'POST a rendered body to a URL.',
  },
  run_command: {
    label: 'Run command',
    icon: 'terminal',
    hint: 'Run a shell command (admin only; must be enabled on the server).',
  },
  run_workflow: {
    label: 'Run workflow',
    icon: 'bolt',
    hint: "Run a published, approved nym workflow as this hook's logic.",
  },
};

/**
 * System actions the backend can return but users cannot author (excluded from
 * HOOK_EVENT_ACTIONS deliberately). Today: the reserved `turn-metadata` hook
 * that GET /hooks synthesizes for every user.
 */
const SYSTEM_ACTION_META: Record<string, HookActionMeta> = {
  turn_metadata: {
    label: 'Turn metadata',
    icon: 'clock',
    hint: 'Built-in [Time:]/[Trigger:] stamp injected at the start of each turn.',
  },
};

const UNKNOWN_ACTION_ICON = 'settings';

/**
 * Meta for ANY action the wire can carry. The typed HOOK_ACTION_META table
 * only knows the nine authorable actions, so feed rows must resolve through
 * this accessor: a system or newer-backend action degrades to a labeled row
 * instead of crashing the dashboard on `undefined.icon`.
 */
export function hookActionMeta(action: HookAction | string): HookActionMeta {
  return (
    HOOK_ACTION_META[action as HookAction] ??
    SYSTEM_ACTION_META[action] ?? { label: action, icon: UNKNOWN_ACTION_ICON, hint: '' }
  );
}

export interface HookEventMeta {
  label: string;
  hint: string;
}

export const HOOK_EVENT_META: Record<HookEvent, HookEventMeta> = {
  prompt_submit: {
    label: 'On your message',
    hint: 'Fires when you send a message, before the agent responds.',
  },
  pre_tool_use: {
    label: 'Before a tool runs',
    hint: 'Fires before a tool call; can block or rewrite it.',
  },
  post_tool_use: {
    label: 'After a tool runs',
    hint: 'Fires after a tool call finishes.',
  },
  done: {
    label: 'When the turn ends',
    hint: "Fires once the agent's reply is complete.",
  },
  command_submit: {
    label: 'Before a command runs',
    hint: 'Fires when a slash command is dispatched, on every surface; can block, rewrite, or require approval.',
  },
};

/** Null-safe twin of hookActionMeta for events a newer backend might add. */
export function hookEventMeta(event: HookEvent | string): HookEventMeta {
  return HOOK_EVENT_META[event as HookEvent] ?? { label: event, hint: '' };
}

export const HOOK_OPERATORS: { value: HookCondition['operator']; label: string }[] = [
  { value: 'contains', label: 'contains' },
  { value: 'equals', label: 'equals' },
  { value: 'not_equals', label: 'not equals' },
  { value: 'starts_with', label: 'starts with' },
  { value: 'matches_regex', label: 'matches regex' },
];

/**
 * Operators for the definition-level fire gate. Superset of HOOK_OPERATORS: adds
 * the numeric comparisons (float-coerced backend-side) the context-usage fields
 * (`context_pct_of_trigger`, `context_tokens`, ...) rely on. Matches the backend
 * `ConditionOperator` set (core/conditions.py).
 */
export const FIRE_GATE_OPERATORS: { value: HookCondition['operator']; label: string }[] = [
  { value: 'contains', label: 'contains' },
  { value: 'equals', label: 'equals' },
  { value: 'not_equals', label: 'not equals' },
  { value: 'starts_with', label: 'starts with' },
  { value: 'matches_regex', label: 'matches regex' },
  { value: 'gt', label: '> (greater than)' },
  { value: 'gte', label: '>= (at least)' },
  { value: 'lt', label: '< (less than)' },
  { value: 'lte', label: '<= (at most)' },
];

/**
 * Short human summary of a hook's logic for the feed row. Reads the flat
 * `logic` dict the backend returns (discriminated on `action`). Switches on
 * the widened action so the wire's system action (`turn_metadata`, outside
 * the authorable `HookAction` union) resolves to its case, not the fallback.
 */
export function describeHookLogic(hook: Hook): string {
  const logic = hook.logic || {};
  const matcher = hook.matcher ? ` on ${hook.matcher}` : '';
  switch (hook.action as HookAction | string) {
    case 'inject_context':
    case 'notify':
    case 'create_todo': {
      const text = String(logic.text ?? hook.text ?? '').replace(/\s+/g, ' ').trim();
      return text || '(empty)';
    }
    case 'webhook': {
      const url = String(logic.url ?? '').trim();
      return url ? `POST ${url}` : '(no URL)';
    }
    case 'block_if_matches': {
      const conds = Array.isArray(logic.conditions) ? logic.conditions : [];
      const when = conds.length
        ? (conds as HookCondition[])
            .map((c) => `${c.field} ${c.operator} ${c.value}`)
            .join(' and ')
        : 'always';
      return `Deny${matcher} when ${when}`;
    }
    case 'rewrite_arg': {
      const updates = (logic.updates ?? {}) as Record<string, string>;
      const keys = Object.keys(updates);
      return keys.length ? `Rewrite ${keys.join(', ')}${matcher}` : `Rewrite args${matcher}`;
    }
    case 'require_approval': {
      const conds = Array.isArray(logic.conditions) ? logic.conditions : [];
      const when = conds.length
        ? (conds as HookCondition[])
            .map((c) => `${c.field} ${c.operator} ${c.value}`)
            .join(' and ')
        : 'always';
      const window = Number(logic.timeout_seconds ?? 180);
      return `Hold for approval${matcher} when ${when} (${window}s window)`;
    }
    case 'run_command': {
      const command = String(logic.command ?? '').replace(/\s+/g, ' ').trim();
      return command ? `Run ${command}${matcher}` : '(no command)';
    }
    case 'run_workflow': {
      const workflowId = String(logic.workflow_id ?? '').trim();
      return workflowId ? `Run workflow ${workflowId}${matcher}` : '(no workflow)';
    }
    case 'turn_metadata': {
      // The system turn-metadata hook: show the template it injects. Newlines
      // are kept (the two-line [Time:]/[Trigger:] frame IS the content;
      // desktop renders the summary pre-wrap, mobile's compact row collapses
      // it to one ellipsized line by design).
      const template = String(logic.text ?? '').trim();
      return template || '(built-in template)';
    }
    default:
      return hook.action;
  }
}
