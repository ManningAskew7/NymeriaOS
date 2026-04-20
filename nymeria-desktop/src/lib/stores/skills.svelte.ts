/**
 * Agent Skills Store
 *
 * Tracks installed skills, marketplace search results, and the user's
 * enabled_global_skills list. Uses the closure-rune pattern like tools.svelte.ts.
 */

import { api } from '$lib/services/api.svelte';
import type {
  SkillMetadata,
  SkillMarketplaceSource,
  MarketplaceSkillEntry,
} from '$lib/types';

const DEFAULT_USER = 'default';

// Installed skills (all scopes)
let installed = $state<SkillMetadata[]>([]);
let installedLoaded = $state(false);
let installedLoading = $state(false);
let installedError = $state<string | null>(null);

// User's enabled_global_skills (applied to every new thread)
let enabledGlobal = $state<string[]>([]);
let enabledGlobalLoaded = $state(false);
let enabledGlobalLoading = $state(false);

// Marketplace search results (last query)
let marketplaceResults = $state<MarketplaceSkillEntry[]>([]);
let marketplaceSearching = $state(false);
let marketplaceError = $state<string | null>(null);
let marketplaceSource = $state<SkillMarketplaceSource>('anthropic');
let marketplaceQuery = $state('');

// Install/uninstall progress (skill name -> 'installing' | 'uninstalling')
let pending = $state<Record<string, 'installing' | 'uninstalling'>>({});

async function loadInstalled(force = false): Promise<void> {
  if (installedLoading) return;
  if (installedLoaded && !force) return;
  installedLoading = true;
  installedError = null;
  try {
    installed = await api.listSkills(DEFAULT_USER);
    installedLoaded = true;
  } catch (e) {
    installedError = e instanceof Error ? e.message : 'Failed to load skills';
    console.error('skills: loadInstalled failed', e);
  } finally {
    installedLoading = false;
  }
}

async function loadGlobal(force = false): Promise<void> {
  if (enabledGlobalLoading) return;
  if (enabledGlobalLoaded && !force) return;
  enabledGlobalLoading = true;
  try {
    enabledGlobal = await api.getGlobalSkills(DEFAULT_USER);
    enabledGlobalLoaded = true;
  } catch (e) {
    console.error('skills: loadGlobal failed', e);
  } finally {
    enabledGlobalLoading = false;
  }
}

async function setGlobalEnabled(names: string[]): Promise<void> {
  try {
    enabledGlobal = await api.setGlobalSkills(names, DEFAULT_USER);
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
    marketplaceError = e instanceof Error ? e.message : 'Marketplace search failed';
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
    const installedSkill = await api.installSkill({ name, source, scope }, DEFAULT_USER);
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
    await api.uninstallSkill(name, scope, DEFAULT_USER);
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

export const skillsStore = {
  // Installed
  get installed() { return installed; },
  get installedLoaded() { return installedLoaded; },
  get installedLoading() { return installedLoading; },
  get installedError() { return installedError; },

  // Global
  get enabledGlobal() { return enabledGlobal; },
  get enabledGlobalLoaded() { return enabledGlobalLoaded; },
  get enabledGlobalLoading() { return enabledGlobalLoading; },

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
  setGlobalEnabled,
  toggleGlobal,
  searchMarketplace,
  install,
  uninstall,
  reset,
};
