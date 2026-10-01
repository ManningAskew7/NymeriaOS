/**
 * Keeps the global stores behind the chat header badges loaded: the model
 * chip (serverSettings), the default-tool count, the trigger count and the
 * global skills. Call once during component initialization (ChatPanel).
 *
 * Tracked deps are the flags a connection switch resets (each store's
 * identity reload hook drops its values and its loaded flag, #242), so the
 * header reloads the new backend's values on its own. It used to track only
 * `isConfigured`, which a switch never flips, and the badges stayed blank
 * until Thread Settings happened to open. The identity is tracked too, and
 * nothing loads without one: a switch resets the stores BEFORE /me names the
 * account, and loads run in that window asked the new backend with no
 * account, failed, then ran again once the hooks fired a second time. Now
 * each runs once, after /me (#242 delta review). Every load latches on failure
 * (`loaded`, `settled`, `enabledGlobalLoaded` turn true with the error), so a
 * failing endpoint is asked once, never in a loop (#381). The loads run
 * untracked so their own `loading` flips cannot re-trigger the effect.
 *
 * Mobile-only file (the drift gate ignores it); desktop's MainPanel carries
 * the same effect inline.
 */

import { untrack } from 'svelte';
import { configStore } from './config.svelte';
import { defaultToolsStore } from './defaultTools.svelte';
import { serverSettingsStore } from './serverSettings.svelte';
import { triggersStore } from './triggers.svelte';
import { skillsStore } from './skills.svelte';

export function keepHeaderStoresLoaded(): void {
  $effect(() => {
    if (!configStore.isConfigured || !configStore.identity) return;
    void defaultToolsStore.loaded;
    void serverSettingsStore.settled;
    void triggersStore.loaded;
    void skillsStore.enabledGlobalLoaded;
    untrack(() => {
      if (!defaultToolsStore.loaded && !defaultToolsStore.loading) void defaultToolsStore.load();
      if (!serverSettingsStore.settled && !serverSettingsStore.loading) void serverSettingsStore.load();
      if (!triggersStore.loaded && !triggersStore.loading) void triggersStore.loadTriggers();
      if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) void skillsStore.loadGlobal();
    });
  });
}
