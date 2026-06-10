/**
 * Agent Skills Store
 *
 * Tracks installed skills, marketplace search results, and the user's
 * enabled_global_skills list. Uses the closure-rune pattern like tools.svelte.ts.
 */

import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type {
  SkillMetadata,
  SkillMarketplaceSource,
  MarketplaceSkillEntry,
} from '$lib/types';

export function createSkillsStore() {
  // Installed skills (all scopes)
  let installed = $state<SkillMetadata[]>([]);
  let installedLoaded = $state(false);
  let installedLoading = $state(false);
  let installedError = $state<string | null>(null);
  let installedRefreshQueued = false;

  // User's enabled_global_skills (applied to every new thread)
  let enabledGlobal = $state<string[]>([]);
  let enabledGlobalLoaded = $state(false);
  let enabledGlobalLoading = $state(false);
  let enabledGlobalError = $state<string | null>(null);
  let enabledGlobalRefreshQueued = false;

  // Marketplace search results (last query)
  let marketplaceResults = $state<MarketplaceSkillEntry[]>([]);
  let marketplaceSearching = $state(false);
  let marketplaceError = $state<string | null>(null);
  let marketplaceSource = $state<SkillMarketplaceSource>('anthropic');
  let marketplaceQuery = $state('');

  // Install/uninstall progress (skill name -> 'installing' | 'uninstalling')
  let pending = $state<Record<string, 'installing' | 'uninstalling'>>({});
  let identityGeneration = 0;

  // Reset on account switch / sign-out — installed skills + global skills are
  // per-user, so the next consumer must re-fetch under the new identity.
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    installed = [];
    installedLoaded = false;
    installedLoading = false;
    installedError = null;
    installedRefreshQueued = false;
    enabledGlobal = [];
    enabledGlobalLoaded = false;
    enabledGlobalLoading = false;
    enabledGlobalError = null;
    enabledGlobalRefreshQueued = false;
    marketplaceResults = [];
    marketplaceSearching = false;
    marketplaceError = null;
    marketplaceQuery = '';
    pending = {};
  });

  async function loadInstalled(force = false): Promise<void> {
    if (installedLoading) {
      if (force) installedRefreshQueued = true;
      return;
    }
    if (installedLoaded && !force) return;
    const requestGeneration = identityGeneration;
    installedLoading = true;
    installedError = null;
    try {
      const nextInstalled = await api.listSkills();
      if (requestGeneration !== identityGeneration) return;
      installed = nextInstalled;
      installedLoaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      installedError = humanizeErrorText(e, { action: 'load', resource: 'your skills' });
      console.error('skills: loadInstalled failed', e);
      // Mark loaded so callers don't re-fire indefinitely on a 404/auth error.
      installedLoaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        installedLoading = false;
        if (installedRefreshQueued) {
          installedRefreshQueued = false;
          void loadInstalled(true);
        }
      }
    }
  }

  async function loadGlobal(force = false): Promise<void> {
    if (enabledGlobalLoading) {
      if (force) enabledGlobalRefreshQueued = true;
      return;
    }
    if (enabledGlobalLoaded && !force) return;
    const requestGeneration = identityGeneration;
    enabledGlobalLoading = true;
    enabledGlobalError = null;
    try {
      const nextEnabledGlobal = await api.getGlobalSkills();
      if (requestGeneration !== identityGeneration) return;
      enabledGlobal = nextEnabledGlobal;
      enabledGlobalLoaded = true;
    } catch (e) {
      if (requestGeneration !== identityGeneration) return;
      enabledGlobalError = humanizeErrorText(e, { action: 'load', resource: 'your global skills' });
      console.error('skills: loadGlobal failed', e);
      // Mark loaded so callers don't re-fire indefinitely on a 404/auth error.
      enabledGlobalLoaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        enabledGlobalLoading = false;
        if (enabledGlobalRefreshQueued) {
          enabledGlobalRefreshQueued = false;
          void loadGlobal(true);
        }
      }
    }
  }

  async function refreshInstalled(): Promise<void> {
    await loadInstalled(true);
  }

  async function refreshGlobal(): Promise<void> {
    await loadGlobal(true);
  }

  async function setGlobalEnabled(names: string[]): Promise<void> {
    try {
      enabledGlobal = await api.setGlobalSkills(names);
    } catch (e) {
      console.error('skills: setGlobalEnabled failed', e);
      throw e;
    }
  }

  async function toggleGlobal(name: string): Promise<void> {
    const next = enabledGlobal.includes(name)
      ? enabledGlobal.filter((n) => n !== name)
      : [...enabledGlobal, name];
    await setGlobalEnabled(next);
  }

  async function searchMarketplace(
    source: SkillMarketplaceSource,
    query: string,
  ): Promise<void> {
    marketplaceSearching = true;
    marketplaceError = null;
    marketplaceSource = source;
    marketplaceQuery = query;
    try {
      marketplaceResults = await api.searchSkillsMarketplace(source, query);
    } catch (e) {
      marketplaceError = humanizeErrorText(e, { action: 'load', resource: 'marketplace results' });
      marketplaceResults = [];
      console.error('skills: searchMarketplace failed', e);
    } finally {
      marketplaceSearching = false;
    }
  }

  async function install(
    name: string,
    source: SkillMarketplaceSource,
    scope: 'user' | 'global' = 'user',
  ): Promise<SkillMetadata | null> {
    pending = { ...pending, [name]: 'installing' };
    try {
      const installedSkill = await api.installSkill({ name, source, scope });
      await loadInstalled(true);
      return installedSkill;
    } catch (e) {
      console.error('skills: install failed', e);
      throw e;
    } finally {
      const { [name]: _, ...rest } = pending;
      pending = rest;
    }
  }

  async function uninstall(name: string, scope: 'user' | 'global'): Promise<void> {
    pending = { ...pending, [name]: 'uninstalling' };
    try {
      await api.uninstallSkill(name, scope);
      await loadInstalled(true);
      // If the uninstalled skill was in enabled_global, reload that list so the
      // UI reflects the server-side cleanup.
      if (enabledGlobal.includes(name)) {
        await loadGlobal(true);
      }
    } catch (e) {
      console.error('skills: uninstall failed', e);
      throw e;
    } finally {
      const { [name]: _, ...rest } = pending;
      pending = rest;
    }
  }

  function isInstalled(name: string): boolean {
    return installed.some((s) => s.name === name);
  }

  function getInstalled(name: string): SkillMetadata | undefined {
    return installed.find((s) => s.name === name);
  }

  function isPending(name: string): 'installing' | 'uninstalling' | undefined {
    return pending[name];
  }

  function reset(): void {
    installedLoaded = false;
    enabledGlobalLoaded = false;
    marketplaceResults = [];
  }

  return {
    // Installed
    get installed() { return installed; },
    get installedLoaded() { return installedLoaded; },
    get installedLoading() { return installedLoading; },
    get installedError() { return installedError; },

    // Global
    get enabledGlobal() { return enabledGlobal; },
    get enabledGlobalLoaded() { return enabledGlobalLoaded; },
    get enabledGlobalLoading() { return enabledGlobalLoading; },
    get enabledGlobalError() { return enabledGlobalError; },

    // Marketplace
    get marketplaceResults() { return marketplaceResults; },
    get marketplaceSearching() { return marketplaceSearching; },
    get marketplaceError() { return marketplaceError; },
    get marketplaceSource() { return marketplaceSource; },
    get marketplaceQuery() { return marketplaceQuery; },

    // Helpers
    isInstalled,
    getInstalled,
    isPending,

    // Actions
    loadInstalled,
    loadGlobal,
    refreshInstalled,
    refreshGlobal,
    setGlobalEnabled,
    toggleGlobal,
    searchMarketplace,
    install,
    uninstall,
    reset,
  };
}

export const skillsStore = createSkillsStore();
