<script lang="ts">
  import type { WorkspaceArtifact } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';

  interface Props {
    artifact: WorkspaceArtifact;
    onClick?: () => void;
  }

  let { artifact, onClick }: Props = $props();

  let loading = $state(true);
  let error = $state<string | null>(null);
  let objectUrl = $state<string | null>(null);

  function revokeObjectUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  // Fetch the workspace file with the Bearer-authenticated client and expose it
  // as a blob URL (the /workspace/download endpoint needs an auth header, so the
  // path cannot be used directly as an <img src>). Mirrors WorkspaceArtifactModal.
  $effect(() => {
    const current = artifact;

    revokeObjectUrl();
    error = null;
    loading = true;
    let cancelled = false;

    void (async () => {
      try {
        const { blob } = await api.downloadWorkspaceFile(current.path);
        if (cancelled) return;
        const nextObjectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(nextObjectUrl);
          return;
        }
        objectUrl = nextObjectUrl;
      } catch (err) {
        if (!cancelled) {
          error = err instanceof Error ? err.message : 'Failed to load image';
        }
      } finally {
        if (!cancelled) {
          loading = false;
        }
      }
    })();

    return () => {
      cancelled = true;
      revokeObjectUrl();
    };
  });
</script>

<div class="workspace-image">
  {#if loading}
    <div class="image-state">
      <Icon name="loading" size={18} />
      <span>Loading image...</span>
    </div>
  {:else if error}
    <div class="image-state error">
      <Icon name="error" size={18} />
      <span>{error}</span>
    </div>
  {:else if objectUrl}
    <button
      type="button"
      class="image-button"
      onclick={onClick}
      title={`View ${artifact.name}`}
    >
      <img src={objectUrl} alt={artifact.name} loading="lazy" />
    </button>
  {/if}
</div>

<style>
  .workspace-image {
    display: flex;
  }

  .image-state {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .image-state.error {
    color: var(--error);
  }

  .image-button {
    display: block;
    padding: 0;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    cursor: pointer;
    overflow: hidden;
    transition: border-color var(--transition-fast), transform var(--transition-fast);
  }

  .image-button:hover {
    border-color: color-mix(in srgb, var(--accent-primary) 50%, var(--border-default));
    transform: translateY(-1px);
  }

  .image-button img {
    display: block;
    max-width: min(420px, 100%);
    max-height: 420px;
    object-fit: contain;
  }
</style>
