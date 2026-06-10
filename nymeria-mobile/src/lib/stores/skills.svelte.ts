import { api } from '$lib/services/api.svelte';
import { humanizeErrorText } from '$lib/services/api/humanizeError';
import { registerIdentityReloadHook } from './config.svelte';
import type { SkillMetadata } from '$lib/types';

function createSkillsStore() {
  let installed = $state<SkillMetadata[]>([]);
  let installedLoaded = $state(false);
  let installedLoading = $state(false);
  let installedError = $state<string | null>(null);

  let enabledGlobal = $state<string[]>([]);
  let enabledGlobalLoaded = $state(false);
  let enabledGlobalLoading = $state(false);
  let identityGeneration = 0;

  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    installed = [];
    installedLoaded = false;
    installedLoading = false;
    installedError = null;
    enabledGlobal = [];
    enabledGlobalLoaded = false;
    enabledGlobalLoading = false;
  });

  async function loadInstalled(force = false): Promise<void> {
    if (installedLoading) return;
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
      installedLoaded = true;
    } finally {
      if (requestGeneration === identityGeneration) {
        installedLoading = false;
      }
    }
  }

  return {
    get installed() { return installed; },
    get installedLoaded() { return installedLoaded; },
    get installedLoading() { return installedLoading; },
    get installedError() { return installedError; },
    get enabledGlobal() { return enabledGlobal; },
    get enabledGlobalLoaded() { return enabledGlobalLoaded; },
    get enabledGlobalLoading() { return enabledGlobalLoading; },

    async loadGlobal(force = false): Promise<void> {
      if (enabledGlobalLoading) return;
      if (enabledGlobalLoaded && !force) return;
      const requestGeneration = identityGeneration;
      enabledGlobalLoading = true;
      try {
        const nextEnabled = await api.getGlobalSkills();
        if (requestGeneration !== identityGeneration) return;
        enabledGlobal = nextEnabled;
        enabledGlobalLoaded = true;
      } catch (e) {
        if (requestGeneration !== identityGeneration) return;
        console.error('skills: loadGlobal failed', e);
        enabledGlobalLoaded = true;
      } finally {
        if (requestGeneration === identityGeneration) {
          enabledGlobalLoading = false;
        }
      }
    },

    reset(): void {
      installedLoaded = false;
      enabledGlobalLoaded = false;
    },

    loadInstalled,
  };
}

export const skillsStore = createSkillsStore();
