import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type { TodoItem, TodoStatus, TodoCreateRequest, TodoUpdateRequest } from '$lib/types';

interface StatusGroup {
  status: TodoStatus;
  label: string;
  todos: TodoItem[];
}

function createTodosStore() {
  let todos = $state<TodoItem[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let lastFetch = $state<Date | null>(null);
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  // Scheduled tasks are per backend: drop them on every connection switch
  // instead of showing the previous backend's until the feed remounts (#242).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    todos = [];
    loading = false;
    error = null;
    lastFetch = null;
  });

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

  const groupedTodos = $derived.by((): StatusGroup[] => {
    const groups: StatusGroup[] = [
      { status: 'in_progress', label: 'In Progress', todos: [] },
      { status: 'pending', label: 'Pending', todos: [] },
      { status: 'done', label: 'Completed', todos: [] }
    ];
    for (const todo of todos) {
      const group = groups.find((g) => g.status === todo.status);
      if (group) { group.todos.push(todo); }
    }
    return groups.filter((g) => g.todos.length > 0);
  });

  const activeCount = $derived(todos.filter((t) => t.status !== 'done').length);

  const scheduledTodos = $derived.by(() => {
    return todos
      .filter((t) => t.scheduledFor && t.status !== 'done')
      .sort((a, b) => {
        const aTime = a.scheduledFor?.getTime() ?? 0;
        const bTime = b.scheduledFor?.getTime() ?? 0;
        return aTime - bTime;
      });
  });

  const scheduledCount = $derived(scheduledTodos.length);
  const nextScheduled = $derived(scheduledTodos.length > 0 ? scheduledTodos[0] : null);

  const organizedTodos = $derived.by(() => {
    const inProgress: TodoItem[] = [];
    const active: TodoItem[] = [];
    const completed: TodoItem[] = [];

    for (const todo of todos) {
      if (todo.status === 'in_progress') { inProgress.push(todo); }
      else if (todo.status === 'done') { completed.push(todo); }
      else { active.push(todo); }
    }

    inProgress.sort((a, b) => {
      const aTime = a.scheduledFor?.getTime() ?? a.createdAt?.getTime() ?? 0;
      const bTime = b.scheduledFor?.getTime() ?? b.createdAt?.getTime() ?? 0;
      return aTime - bTime;
    });

    active.sort((a, b) => {
      const aTime = a.scheduledFor?.getTime() ?? a.createdAt?.getTime() ?? 0;
      const bTime = b.scheduledFor?.getTime() ?? b.createdAt?.getTime() ?? 0;
      return aTime - bTime;
    });

    completed.sort((a, b) => {
      const aTime = a.updatedAt?.getTime() ?? 0;
      const bTime = b.updatedAt?.getTime() ?? 0;
      return bTime - aTime;
    });

    return { inProgress, active, completed };
  });

  const completedCount = $derived(todos.filter((t) => t.status === 'done').length);

  let currentThreadFilter = $state<string | undefined>(undefined);
  let currentStatusFilter = $state<string | undefined>(undefined);

  async function fetch(filterStatus?: string, threadId?: string): Promise<void> {
    const generation = identityGeneration;
    loading = true;
    error = null;
    currentStatusFilter = filterStatus;
    currentThreadFilter = threadId;
    try {
      const response = await api.getTodos(filterStatus, currentThreadFilter);
      if (!current(generation)) return;
      todos = response.items;
      lastFetch = new Date();
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your scheduled tasks' });
      console.error('Failed to fetch TODOs:', e);
    } finally {
      if (current(generation)) loading = false;
    }
  }

  function clear(): void {
    todos = [];
    error = null;
    lastFetch = null;
  }

  async function create(request: TodoCreateRequest): Promise<TodoItem> {
    const generation = identityGeneration;
    try {
      const newTodo = await api.createTodo(request);
      if (current(generation)) await fetch();
      return newTodo;
    } catch (e) {
      error = humanizeErrorText(e, { action: 'create', resource: 'the task' });
      console.error('Failed to create TODO:', e);
      throw e;
    }
  }

  async function update(todoId: string, request: TodoUpdateRequest): Promise<TodoItem> {
    const generation = identityGeneration;
    try {
      const updatedTodo = await api.updateTodo(todoId, request);
      if (current(generation)) await fetch();
      return updatedTodo;
    } catch (e) {
      error = humanizeErrorText(e, { action: 'update', resource: 'the task' });
      console.error('Failed to update TODO:', e);
      throw e;
    }
  }

  async function deleteTodo(todoId: string): Promise<void> {
    const generation = identityGeneration;
    try {
      await api.deleteTodo(todoId);
      if (current(generation)) todos = todos.filter((t) => t.id !== todoId);
    } catch (e) {
      error = humanizeErrorText(e, { action: 'delete', resource: 'the task' });
      console.error('Failed to delete TODO:', e);
      throw e;
    }
  }

  async function complete(todoId: string): Promise<TodoItem> {
    const generation = identityGeneration;
    try {
      const completedTodo = await api.completeTodo(todoId);
      if (current(generation)) await fetch();
      return completedTodo;
    } catch (e) {
      error = humanizeErrorText(e, { action: 'update', resource: 'the task' });
      console.error('Failed to complete TODO:', e);
      throw e;
    }
  }

  return {
    get todos() { return todos; },
    get groupedTodos() { return groupedTodos; },
    get activeCount() { return activeCount; },
    get scheduledTodos() { return scheduledTodos; },
    get scheduledCount() { return scheduledCount; },
    get nextScheduled() { return nextScheduled; },
    get organizedTodos() { return organizedTodos; },
    get completedCount() { return completedCount; },
    get loading() { return loading; },
    get error() { return error; },
    get lastFetch() { return lastFetch; },
    fetch,
    clear,
    create,
    update,
    delete: deleteTodo,
    complete,
    onTodoToolCompleted: () => fetch(currentStatusFilter, currentThreadFilter)
  };
}

export const todosStore = createTodosStore();
