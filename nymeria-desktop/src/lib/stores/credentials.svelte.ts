import { api } from '$lib/services/api.svelte';
import type {
  Credential,
  CredentialBinding,
  CredentialBindingRequest,
  CredentialCreateRequest,
  CredentialUpdateRequest
} from '$lib/types';

function createCredentialsStore() {
  let credentials = $state<Credential[]>([]);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let bindings = $state<Record<string, CredentialBinding[]>>({});

  async function load(scope: 'visible' | 'mine' | 'system' | 'all' = 'visible'): Promise<void> {
    if (loading) return;
    loading = true;
    error = null;
    try {
      const response = await api.listCredentials(scope);
      credentials = response.credentials;
      loaded = true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load credentials';
      console.error('Failed to load credentials:', e);
    } finally {
      loading = false;
    }
  }

  async function refresh(): Promise<void> {
    loaded = false;
    await load();
  }

  async function create(request: CredentialCreateRequest): Promise<Credential | null> {
    loading = true;
    error = null;
    try {
      const credential = await api.createCredential(request);
      credentials = [credential, ...credentials.filter((c) => c.id !== credential.id)];
      return credential;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to create credential';
      return null;
    } finally {
      loading = false;
    }
  }

  async function update(credentialId: string, request: CredentialUpdateRequest): Promise<Credential | null> {
    loading = true;
    error = null;
    try {
      const credential = await api.updateCredential(credentialId, request);
      credentials = credentials.map((c) => c.id === credentialId ? credential : c);
      return credential;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to update credential';
      return null;
    } finally {
      loading = false;
    }
  }

  async function disable(credentialId: string): Promise<boolean> {
    loading = true;
    error = null;
    try {
      await api.deleteCredential(credentialId);
      credentials = credentials.map((c) => c.id === credentialId ? { ...c, status: 'disabled' } : c);
      return true;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to disable credential';
      return false;
    } finally {
      loading = false;
    }
  }

  async function test(credentialId: string): Promise<Credential | null> {
    try {
      const credential = await api.testCredential(credentialId);
      credentials = credentials.map((c) => c.id === credentialId ? credential : c);
      return credential;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to test credential';
      return null;
    }
  }

  async function loadBindings(credentialId: string): Promise<CredentialBinding[]> {
    try {
      const rows = await api.listCredentialBindings(credentialId);
      bindings = { ...bindings, [credentialId]: rows };
      return rows;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load credential bindings';
      return [];
    }
  }

  async function bind(credentialId: string, request: CredentialBindingRequest): Promise<CredentialBinding | null> {
    try {
      const row = await api.bindCredential(credentialId, request);
      bindings = { ...bindings, [credentialId]: [row, ...(bindings[credentialId] || [])] };
      return row;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to bind credential';
      return null;
    }
  }

  return {
    get credentials() { return credentials; },
    get loading() { return loading; },
    get loaded() { return loaded; },
    get error() { return error; },
    get bindings() { return bindings; },
    load,
    refresh,
    create,
    update,
    disable,
    test,
    loadBindings,
    bind,
  };
}

export const credentialsStore = createCredentialsStore();
