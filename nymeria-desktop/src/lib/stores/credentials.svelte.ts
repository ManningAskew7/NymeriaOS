import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
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
  // Bumped by the reload hook: a response from the previous backend lands nowhere.
  let identityGeneration = 0;

  // The vault is per backend and per account: drop it on every connection
  // switch so the panel reloads from the new backend instead of listing (and
  // acting on) the previous one's credentials (#242).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    credentials = [];
    bindings = {};
    loading = false;
    loaded = false;
    error = null;
  });

  function current(generation: number): boolean {
    return generation === identityGeneration;
  }

  async function load(scope: 'visible' | 'mine' | 'system' | 'all' = 'visible'): Promise<void> {
    if (loading) return;
    const generation = identityGeneration;
    loading = true;
    error = null;
    try {
      const response = await api.listCredentials(scope);
      if (!current(generation)) return;
      credentials = response.credentials;
      loaded = true;
    } catch (e) {
      if (!current(generation)) return;
      error = humanizeErrorText(e, { action: 'load', resource: 'your credentials' });
      console.error('Failed to load credentials:', e);
      // Settle on failure too: the panel's load effect gates on `loaded`, so
      // leaving it false re-ran the request as fast as the backend refused
      // it. refresh() (the panel's Refresh button) asks again.
      loaded = true;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function refresh(): Promise<void> {
    loaded = false;
    await load();
  }

  async function create(request: CredentialCreateRequest): Promise<Credential | null> {
    const generation = identityGeneration;
    loading = true;
    error = null;
    try {
      const credential = await api.createCredential(request);
      if (!current(generation)) return null;
      credentials = [credential, ...credentials.filter((c) => c.id !== credential.id)];
      return credential;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'create', resource: 'the credential' });
      return null;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function update(credentialId: string, request: CredentialUpdateRequest): Promise<Credential | null> {
    const generation = identityGeneration;
    loading = true;
    error = null;
    try {
      const credential = await api.updateCredential(credentialId, request);
      if (!current(generation)) return null;
      credentials = credentials.map((c) => c.id === credentialId ? credential : c);
      return credential;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'update', resource: 'the credential' });
      return null;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function disable(credentialId: string): Promise<boolean> {
    const generation = identityGeneration;
    loading = true;
    error = null;
    try {
      await api.deleteCredential(credentialId);
      if (!current(generation)) return false;
      credentials = credentials.map((c) => c.id === credentialId ? { ...c, status: 'disabled' } : c);
      return true;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'disable', resource: 'the credential' });
      return false;
    } finally {
      if (current(generation)) loading = false;
    }
  }

  async function test(credentialId: string): Promise<Credential | null> {
    const generation = identityGeneration;
    try {
      const credential = await api.testCredential(credentialId);
      if (!current(generation)) return null;
      credentials = credentials.map((c) => c.id === credentialId ? credential : c);
      return credential;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'test', resource: 'the credential' });
      return null;
    }
  }

  async function loadBindings(credentialId: string): Promise<CredentialBinding[]> {
    const generation = identityGeneration;
    try {
      const rows = await api.listCredentialBindings(credentialId);
      if (!current(generation)) return [];
      bindings = { ...bindings, [credentialId]: rows };
      return rows;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'load', resource: "the credential's bindings" });
      return [];
    }
  }

  async function bind(credentialId: string, request: CredentialBindingRequest): Promise<CredentialBinding | null> {
    const generation = identityGeneration;
    try {
      const row = await api.bindCredential(credentialId, request);
      if (!current(generation)) return null;
      bindings = { ...bindings, [credentialId]: [row, ...(bindings[credentialId] || [])] };
      return row;
    } catch (e) {
      if (current(generation)) error = humanizeErrorText(e, { action: 'save', resource: 'the credential binding' });
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
