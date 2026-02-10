import { api } from '$lib/services/api.svelte';
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

  // Computed: group todos by status
  const groupedTodos = $derived.by((): StatusGroup[] => {
    const groups: StatusGroup[] = [
      { status: 'in_progress', label: 'In Progress', todos: [] },
      { status: 'blocked', label: 'Blocked', todos: [] },
      { status: 'pending', label: 'Pending', todos: [] },
      { status: 'done', label: 'Completed', todos: [] }
    ];

    for (const todo of todos) {
      const group = groups.find((g) => g.status === todo.status);
      if (group) {
        group.todos.push(todo);
      }
    }

    return groups.filter((g) => g.todos.length > 0);
  });

  // Computed: count of active (non-done) todos
  const activeCount = $derived(todos.filter((t) => t.status !== 'done').length);

  // Computed: todos with scheduled execution times (sorted by scheduledFor)
  const scheduledTodos = $derived.by(() => {
    return todos
      .filter((t) => t.scheduledFor && t.status !== 'done')
      .sort((a, b) => {
        const aTime = a.scheduledFor?.getTime() ?? 0;
        const bTime = b.scheduledFor?.getTime() ?? 0;
        return aTime - bTime;
      });
  });

  // Computed: count of scheduled todos
  const scheduledCount = $derived(scheduledTodos.length);

  // Computed: next scheduled todo
  const nextScheduled = $derived(scheduledTodos.length > 0 ? scheduledTodos[0] : null);

  // Computed: organized todos for dashboard display
  // - In-progress at top (highlighted)
  // - Active (pending/blocked) sorted chronologically by scheduledFor or createdAt
  // - Completed at bottom sorted chronologically (newest first)
  const organizedTodos = $derived.by(() => {
    const inProgress: TodoItem[] = [];
    const active: TodoItem[] = [];
    const completed: TodoItem[] = [];

    for (const todo of todos) {
      if (todo.status === 'in_progress') {
        inProgress.push(todo);
      } else if (todo.status === 'done') {
        completed.push(todo);
      } else {
        // pending or blocked
        active.push(todo);
      }
    }

    // Sort in-progress by scheduledFor (soonest first), then by createdAt
    inProgress.sort((a, b) => {
      const aTime = a.scheduledFor?.getTime() ?? a.createdAt?.getTime() ?? 0;
      const bTime = b.scheduledFor?.getTime() ?? b.createdAt?.getTime() ?? 0;
      return aTime - bTime;
    });

    // Sort active by scheduledFor (soonest first), then by createdAt
    active.sort((a, b) => {
      const aTime = a.scheduledFor?.getTime() ?? a.createdAt?.getTime() ?? 0;
      const bTime = b.scheduledFor?.getTime() ?? b.createdAt?.getTime() ?? 0;
      return aTime - bTime;
    });

    // Sort completed by updatedAt (newest first)
    completed.sort((a, b) => {
      const aTime = a.updatedAt?.getTime() ?? 0;
      const bTime = b.updatedAt?.getTime() ?? 0;
      return bTime - aTime;
    });

    return { inProgress, active, completed };
  });

  // Computed: count of completed todos
  const completedCount = $derived(todos.filter((t) => t.status === 'done').length);

  let currentThreadFilter = $state<string | undefined>(undefined);

  async function fetch(filterStatus?: string, threadId?: string): Promise<void> {
    loading = true;
    error = null;
    currentThreadFilter = threadId;

    try {
      const response = await api.getTodos(filterStatus, currentThreadFilter);
      todos = response.items;
      lastFetch = new Date();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to fetch TODOs';
      console.error('Failed to fetch TODOs:', e);
    } finally {
      loading = false;
    }
  }

  function clear(): void {
    todos = [];
    error = null;
    lastFetch = null;
  }

  async function create(request: TodoCreateRequest): Promise<TodoItem> {
    try {
      const newTodo = await api.createTodo(request);
      // Refresh the list to get the new todo
      await fetch();
      return newTodo;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to create TODO';
      console.error('Failed to create TODO:', e);
      throw e;
    }
  }

  async function update(todoId: string, request: TodoUpdateRequest): Promise<TodoItem> {
    try {
      const updatedTodo = await api.updateTodo(todoId, request);
      // Refresh the list to get the updated todo
      await fetch();
      return updatedTodo;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update TODO';
      console.error('Failed to update TODO:', e);
      throw e;
    }
  }

  async function deleteTodo(todoId: string): Promise<void> {
    try {
      await api.deleteTodo(todoId);
      // Remove from local state immediately
      todos = todos.filter((t) => t.id !== todoId);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to delete TODO';
      console.error('Failed to delete TODO:', e);
      throw e;
    }
  }

  async function complete(todoId: string): Promise<TodoItem> {
    try {
      const completedTodo = await api.completeTodo(todoId);
      // Refresh the list
      await fetch();
      return completedTodo;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to complete TODO';
      console.error('Failed to complete TODO:', e);
      throw e;
    }
  }

  return {
    get todos() {
      return todos;
    },
    get groupedTodos() {
      return groupedTodos;
    },
    get activeCount() {
      return activeCount;
    },
    get scheduledTodos() {
      return scheduledTodos;
    },
    get scheduledCount() {
      return scheduledCount;
    },
    get nextScheduled() {
      return nextScheduled;
    },
    get organizedTodos() {
      return organizedTodos;
    },
    get completedCount() {
      return completedCount;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get lastFetch() {
      return lastFetch;
    },
    fetch,
    clear,
    create,
    update,
    delete: deleteTodo,
    complete,
    // Refresh when a todo tool completes
    onTodoToolCompleted: () => fetch()
  };
}

export const todosStore = createTodosStore();
