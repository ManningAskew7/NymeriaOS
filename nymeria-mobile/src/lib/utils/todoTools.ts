const TODO_TOOL_NAMES = new Set([
  'nym_todo',
  'nym_todo_delete',
  'nym_todo_list',
  'todo',
  'todo_delete',
  'todo_list',
  'watchdog_dispatch',
  'watchdog_todo_overview',
]);

export function isTodoTool(name: string | null | undefined): boolean {
  const normalized = (name || '').trim().toLowerCase();
  if (!normalized) return false;
  return (
    TODO_TOOL_NAMES.has(normalized) ||
    normalized.startsWith('nym_todo_') ||
    normalized.startsWith('todo_') ||
    normalized.startsWith('nymeria_todo_') ||
    normalized.includes('__nymeria_todo_')
  );
}
