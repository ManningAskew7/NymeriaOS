<script lang="ts">
  import type { ToolReloadInfo } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    info: ToolReloadInfo;
  }

  let { info }: Props = $props();

  const toolNames = $derived(
    info.tools.length > 0 ? info.tools.join(', ') : ''
  );

  const isSkillKit = $derived(info.source === 'skill_kit');
  const isSkillWrite = $derived(info.source === 'skill_write');
  const isSkillEdit = $derived(info.source === 'skill_edit');
  const isSkillInstall = $derived(info.source === 'skill_install');
  const isMcpInstall = $derived(info.source === 'mcp_install');
  const isToolCreate = $derived(info.source === 'tool_create');
  const labelText = $derived(
    isMcpInstall
      ? 'MCP Tools Installed'
      : isSkillInstall
        ? 'Skill Enabled'
        : isSkillEdit
          ? 'Skill Updated'
          : isSkillWrite
            ? 'Skill Published'
            : isSkillKit
              ? 'Skill Kit Binding'
              : isToolCreate
                ? 'Tool Created'
                : 'Tool Binding'
  );
  const sourceText = $derived(
    isSkillKit && info.skillName
      ? `Skill Kit "${info.skillName}"`
      : isSkillInstall && info.skillName
        ? `skill_manage enabling Skill "${info.skillName}"`
        : isSkillInstall
          ? 'skill_manage'
          : isSkillWrite && info.skillName
            ? `skill_write publishing Skill "${info.skillName}"`
            : isSkillWrite
              ? 'skill_write'
              : isSkillEdit && info.skillName
                ? `skill_edit updating Skill "${info.skillName}"`
                : isSkillEdit
                  ? 'skill_edit'
                  : isMcpInstall
                    ? 'MCP server installation'
                    : isToolCreate
                      ? 'tool_create publishing a new tool'
                      : 'tool_manage(action="enable")'
  );

  const ttlText = $derived(ttlPhrase(info.ttlSeconds, info.ttl));

  function ttlPhrase(ttlSeconds: number | null | undefined, ttl: string): string {
    if (ttl === 'permanent') return 'permanently';
    if (typeof ttlSeconds === 'number') {
      const hours = Math.floor(ttlSeconds / 3600);
      const minutes = Math.floor((ttlSeconds % 3600) / 60);
      if (hours > 0 && minutes > 0) return `for the next ${hours}h ${minutes}m`;
      if (hours > 0) return `for the next ${hours}h`;
      return `for the next ${minutes}m`;
    }
    return ttl ? `for the next ${ttl}` : 'for the configured TTL';
  }

  const resumeText = $derived(
    info.resumePrompt ||
    (info.tools.length > 0
      ? `[System: tool reload complete. The following tools are now bound to you ${ttlText}: ${info.tools.join(', ')}. This is the automatic resume after ${sourceText}.${info.reason ? ` Reason: ${info.reason}.` : ''} Continue the user's original task now; you may call these newly-loaded tools in this resumed step.]`
      : `[System: capability reload complete. The thread's skill list and tool schemas have been refreshed. This is the automatic resume after ${sourceText}.${info.reason ? ` Reason: ${info.reason}.` : ''} Continue the user's original task now.]`)
  );

  const metaText = $derived(
    toolNames
      ? `${toolNames}${isSkillWrite || isSkillEdit ? '' : ` (${ttlText})`}`
      : isSkillKit && info.skillName
        ? `from ${sourceText}`
        : sourceText
  );
</script>

<div class="reload-message">
  <div class="reload-header">
    <span class="icon">
      <Icon name="cog" size={14} />
    </span>
    <span class="label">{labelText}</span>
    {#if metaText}
      <span class="meta">{metaText}</span>
    {/if}
  </div>
  <div class="resume-text">{resumeText}</div>
</div>

<style>
  .reload-message {
    align-self: flex-start;
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: min(760px, 88%);
    margin: var(--spacing-xs) 0 var(--spacing-md);
    padding: 10px 12px;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--bg-elevated) 88%, var(--accent-primary));
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    animation: fadeSlide 150ms ease-out;
  }

  .reload-header {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .icon {
    display: flex;
    align-items: center;
    color: var(--accent-primary);
  }

  .label {
    font-weight: 600;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
  }

  .meta {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .resume-text {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    line-height: 1.5;
    color: var(--text-secondary);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  @keyframes fadeSlide {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
