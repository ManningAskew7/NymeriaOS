import { skillsStore } from '$lib/stores/skills.svelte';
import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
import { threadsStore } from '$lib/stores/threads.svelte';

const SKILL_MUTATION_TOOL_NAMES = new Set([
  'install_skill',
  'skill_edit',
  'skill_manage',
  'skill_write',
]);

const SKILL_MUTATION_RELOAD_SOURCES = new Set([
  'skill_edit',
  'skill_install',
  'skill_kit',
  'skill_write',
]);

export function isSkillMutationToolName(name?: string | null): boolean {
  return !!name && SKILL_MUTATION_TOOL_NAMES.has(name);
}

export function isSkillMutationReloadSource(source?: string | null): boolean {
  return !!source && SKILL_MUTATION_RELOAD_SOURCES.has(source);
}

export function refreshSkillStateAfterMutation(threadId?: string | null): void {
  void skillsStore.refreshInstalled();
  void skillsStore.refreshGlobal();

  const targetThreadId = threadId || threadsStore.currentThreadId;
  if (targetThreadId) {
    void threadConfigStore.loadConfig(targetThreadId).catch((err) => {
      console.warn('[skillRefresh] Failed to refresh thread config after skill mutation:', err);
    });
  }
}
