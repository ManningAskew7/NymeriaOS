import { api } from '$lib/services/api.svelte';
import { registerIdentityReloadHook } from './config.svelte';

function createSkillsStore() {
  let enabledGlobal = $state<string[]>([]);
  let enabledGlobalLoaded = $state(false);
  let enabledGlobalLoading = $state(false);
  let identityGeneration = 0;

  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    enabledGlobal = [];
    enabledGlobalLoaded = false;
    enabledGlobalLoading = false;
  });

  return {
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
      enabledGlobalLoaded = false;
    },
  };
}

export const skillsStore = createSkillsStore();
