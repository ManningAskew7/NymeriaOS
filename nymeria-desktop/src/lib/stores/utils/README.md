# Store Utilities

Reusable store factory patterns for Svelte 5 runes.

## Available Utilities

### `polling.ts` - Polling Store Factory

Creates stores that periodically fetch data from an API endpoint.

```typescript
import { createPollingStore } from './polling';

const activityStore = createPollingStore({
  fetchFn: () => api.getActivity(),
  extractItems: (r) => r.entries,
  pollInterval: 30000,  // 30 seconds
  errorName: 'activity'
});

// Usage
activityStore.startPolling();
activityStore.stopPolling();
activityStore.fetch();  // Manual fetch
```

**Features:**
- Automatic polling at configurable intervals
- Loading and error state management
- Item count via `extractItems` function
- `lastFetch` timestamp tracking

### `crud-store.ts` - CRUD Store Factory

Creates stores for managing collections with create, read, update, delete operations.

```typescript
import { createCrudStore } from './crud-store';

const toolsStore = createCrudStore({
  name: 'tool',
  idField: 'id',
  loadFn: () => api.getCustomTools(),
  createFn: (req) => api.createCustomTool(req),
  updateFn: (id, req) => api.updateCustomTool(id, req),
  deleteFn: (id) => api.deleteCustomTool(id),
  testFn: (id, params) => api.testCustomTool(id, params),
  extractItems: (r) => r.tools
});

// Usage
await toolsStore.load();
await toolsStore.create({ name: 'New Tool', ... });
await toolsStore.update(id, { name: 'Updated' });
await toolsStore.delete(id);
toolsStore.select(id);
```

**Features:**
- Full CRUD operations with loading states
- Selection tracking (`selectedId`, `selectedItem`)
- Enabled/disabled filtering (`getEnabled()`, `getDisabled()`)
- Optional test function support
- `toggleEnabled()` helper for enable/disable toggles

## Migration Notes

### Removed: `tasks.svelte.ts`

The `tasks.svelte.ts` store has been **removed** as part of the migration from the legacy `self_invoke` scheduler to TODO-based scheduling.

**Before (deprecated):**
```typescript
import { tasksStore } from '$lib/stores/tasks.svelte';
const scheduled = tasksStore.scheduled;
```

**After:**
```typescript
import { todosStore } from '$lib/stores/todos.svelte';
const scheduled = todosStore.scheduledTodos;
```

The `todosStore` provides:
- `scheduledTodos`: TODOs with a `scheduled_for` datetime
- Full TODO management (add, update, complete, delete)
- Automatic sorting by priority and status

## Store Patterns

All stores follow these patterns:

1. **Reactive State**: Use `$state` for mutable state, `$derived` for computed values
2. **Loading States**: Track `loading` and `loaded` flags
3. **Error Handling**: Store errors in `error` property, clear with `clearError()`
4. **Getters**: Expose state via getters for reactivity

```typescript
// Pattern example
const store = {
  get items() { return items; },
  get loading() { return loading; },
  get error() { return error; },
  // ...methods
};
```
