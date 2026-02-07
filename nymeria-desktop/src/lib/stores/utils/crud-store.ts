/**
 * Reusable CRUD store factory for Svelte 5 runes.
 *
 * Creates a store that manages a collection of items with:
 * - Load, create, update, delete operations
 * - Loading and error state management
 * - Selection tracking
 * - Enabled/disabled filtering
 */

export interface CrudStoreConfig<T, CreateReq, UpdateReq, TestResp> {
  /** Name for error messages */
  name: string;
  /** Unique identifier field */
  idField: keyof T;
  /** API function to load all items */
  loadFn: () => Promise<{ items: T[] } | T[]>;
  /** API function to create an item */
  createFn: (request: CreateReq) => Promise<T>;
  /** API function to update an item */
  updateFn: (id: string, request: UpdateReq) => Promise<T>;
  /** API function to delete an item */
  deleteFn: (id: string) => Promise<void>;
  /** API function to test an item (optional) */
  testFn?: (id: string, params: unknown) => Promise<TestResp>;
  /** Function to extract items array from load response (if not directly an array) */
  extractItems?: (response: unknown) => T[];
}

export interface CrudStore<T, CreateReq, UpdateReq, TestResp> {
  readonly items: T[];
  readonly loading: boolean;
  readonly loaded: boolean;
  readonly error: string | null;
  readonly selectedId: string | null;
  readonly selectedItem: T | undefined;
  getById(id: string): T | undefined;
  getEnabled(): T[];
  getDisabled(): T[];
  load(): Promise<void>;
  resetLoaded(): void;
  create(request: CreateReq): Promise<T | null>;
  update(id: string, request: UpdateReq): Promise<T | null>;
  delete(id: string): Promise<boolean>;
  test?(id: string, params: unknown): Promise<TestResp | null>;
  toggleEnabled?(id: string): Promise<boolean>;
  select(id: string | null): void;
  clearError(): void;
}

/**
 * Create a CRUD store with Svelte 5 runes.
 *
 * @example
 * ```ts
 * const toolsStore = createCrudStore({
 *   name: 'tool',
 *   idField: 'id',
 *   loadFn: () => api.getCustomTools(),
 *   createFn: (req) => api.createCustomTool(req),
 *   updateFn: (id, req) => api.updateCustomTool(id, req),
 *   deleteFn: (id) => api.deleteCustomTool(id),
 *   extractItems: (r) => r.tools
 * });
 * ```
 */
export function createCrudStore<
  T extends { enabled?: boolean },
  CreateReq,
  UpdateReq,
  TestResp = unknown
>(
  config: CrudStoreConfig<T, CreateReq, UpdateReq, TestResp>
): CrudStore<T, CreateReq, UpdateReq, TestResp> {
  const { name, idField, loadFn, createFn, updateFn, deleteFn, testFn, extractItems } = config;

  let items = $state<T[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let selectedId = $state<string | null>(null);

  const selectedItem = $derived(
    items.find((item) => String(item[idField]) === selectedId)
  );

  function getById(id: string): T | undefined {
    return items.find((item) => String(item[idField]) === id);
  }

  function getEnabled(): T[] {
    return items.filter((item) => item.enabled);
  }

  function getDisabled(): T[] {
    return items.filter((item) => !item.enabled);
  }

  async function load(): Promise<void> {
    if (loading || loaded) {
      return;
    }

    loading = true;
    error = null;

    try {
      const response = await loadFn();
      if (extractItems) {
        items = extractItems(response);
      } else if (Array.isArray(response)) {
        items = response;
      } else {
        items = (response as { items: T[] }).items;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to load ${name}s`;
      console.error(`Failed to load ${name}s:`, e);
    } finally {
      loading = false;
      loaded = true;
    }
  }

  function resetLoaded(): void {
    loaded = false;
  }

  async function create(request: CreateReq): Promise<T | null> {
    loading = true;
    error = null;

    try {
      const newItem = await createFn(request);
      items = [...items, newItem];
      return newItem;
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to create ${name}`;
      console.error(`Failed to create ${name}:`, e);
      return null;
    } finally {
      loading = false;
    }
  }

  async function update(id: string, request: UpdateReq): Promise<T | null> {
    loading = true;
    error = null;

    try {
      const updatedItem = await updateFn(id, request);
      items = items.map((item) =>
        String(item[idField]) === id ? updatedItem : item
      );
      return updatedItem;
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to update ${name}`;
      console.error(`Failed to update ${name}:`, e);
      return null;
    } finally {
      loading = false;
    }
  }

  async function deleteItem(id: string): Promise<boolean> {
    loading = true;
    error = null;

    try {
      await deleteFn(id);
      items = items.filter((item) => String(item[idField]) !== id);

      // Clear selection if deleted
      if (selectedId === id) {
        selectedId = null;
      }

      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to delete ${name}`;
      console.error(`Failed to delete ${name}:`, e);
      return false;
    } finally {
      loading = false;
    }
  }

  async function test(id: string, params: unknown): Promise<TestResp | null> {
    if (!testFn) return null;

    loading = true;
    error = null;

    try {
      const result = await testFn(id, params);
      return result;
    } catch (e) {
      error = e instanceof Error ? e.message : `Failed to test ${name}`;
      console.error(`Failed to test ${name}:`, e);
      return null;
    } finally {
      loading = false;
    }
  }

  async function toggleEnabled(id: string): Promise<boolean> {
    const item = getById(id);
    if (!item || item.enabled === undefined) return false;

    const updated = await update(id, { enabled: !item.enabled } as unknown as UpdateReq);
    return updated !== null;
  }

  function select(id: string | null): void {
    selectedId = id;
  }

  function clearError(): void {
    error = null;
  }

  const store: CrudStore<T, CreateReq, UpdateReq, TestResp> = {
    get items() {
      return items;
    },
    get loading() {
      return loading;
    },
    get loaded() {
      return loaded;
    },
    get error() {
      return error;
    },
    get selectedId() {
      return selectedId;
    },
    get selectedItem() {
      return selectedItem;
    },
    getById,
    getEnabled,
    getDisabled,
    load,
    resetLoaded,
    create,
    update,
    delete: deleteItem,
    select,
    clearError
  };

  // Add optional methods
  if (testFn) {
    store.test = test;
  }
  store.toggleEnabled = toggleEnabled;

  return store;
}
