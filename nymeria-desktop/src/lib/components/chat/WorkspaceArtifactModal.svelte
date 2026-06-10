<script lang="ts">
  import type { WorkspaceArtifact } from '$lib/types';
  import { Icon, Modal } from '$lib/components/common';
  import { api } from '$lib/services/api.svelte';
  import { formatFileSize } from '$lib/utils/fileProcessing';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

  interface Props {
    artifact: WorkspaceArtifact | null;
    onClose: () => void;
  }

  let { artifact, onClose }: Props = $props();

  let loading = $state(false);
  let error = $state<string | null>(null);
  let objectUrl = $state<string | null>(null);
  let textContent = $state('');
  let blobData = $state<Blob | null>(null);
  let resolvedFilename = $state('');
  let resolvedContentType = $state('');

  function revokeObjectUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  function isImageType(mimeType: string): boolean {
    return mimeType.startsWith('image/');
  }

  function isPdfType(mimeType: string): boolean {
    return mimeType === 'application/pdf';
  }

  function isTextLike(mimeType: string, fileName: string): boolean {
    const normalizedMime = mimeType.toLowerCase();
    const normalizedName = fileName.toLowerCase();

    if (normalizedMime.startsWith('text/')) return true;
    if ([
      'application/json',
      'application/xml',
      'application/yaml',
      'application/x-yaml'
    ].includes(normalizedMime)) {
      return true;
    }

    return /\.(txt|md|markdown|csv|json|log|py|ts|tsx|js|jsx|html|css|xml|yaml|yml|sql|sh)$/i.test(normalizedName);
  }

  async function handleDownload() {
    if (!artifact || !blobData) return;

    const downloadUrl = objectUrl || URL.createObjectURL(blobData);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = resolvedFilename || artifact.name;
    document.body.appendChild(link);
    link.click();
    link.remove();

    if (!objectUrl) {
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    }
  }

  function handleOpenRaw() {
    if (objectUrl) {
      window.open(objectUrl, '_blank', 'noopener,noreferrer');
    }
  }

  $effect(() => {
    const currentArtifact = artifact;

    revokeObjectUrl();
    error = null;
    textContent = '';
    blobData = null;
    resolvedFilename = currentArtifact?.name || '';
    resolvedContentType = currentArtifact?.mimeType || '';

    if (!currentArtifact) {
      loading = false;
      return;
    }

    let cancelled = false;
    loading = true;

    void (async () => {
      try {
        const { blob, filename, contentType } = await api.downloadWorkspaceFile(currentArtifact.path);
        if (cancelled) return;

        blobData = blob;
        resolvedFilename = filename;
        resolvedContentType = contentType || currentArtifact.mimeType;

        if (isImageType(resolvedContentType) || isPdfType(resolvedContentType)) {
          const nextObjectUrl = URL.createObjectURL(blob);
          if (cancelled) {
            URL.revokeObjectURL(nextObjectUrl);
            return;
          }
          objectUrl = nextObjectUrl;
        } else if (isTextLike(resolvedContentType, filename)) {
          const nextTextContent = await blob.text();
          if (cancelled) return;
          textContent = nextTextContent;
        }
      } catch (err) {
        if (!cancelled) {
          error = humanizeErrorText(err, { action: 'load', resource: 'the file' });
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

<Modal
  title={artifact ? `File Viewer: ${artifact.name}` : 'File Viewer'}
  isOpen={!!artifact}
  {onClose}
>
  {#snippet children()}
    {#if artifact}
      <div class="artifact-viewer">
        <div class="artifact-toolbar">
          <div class="artifact-meta">
            <div class="artifact-name-row">
              <Icon name={isImageType(resolvedContentType || artifact.mimeType) ? 'image' : 'fileText'} size={18} />
              <strong>{resolvedFilename || artifact.name}</strong>
            </div>
            <div class="artifact-details">
              <span>{formatFileSize(artifact.sizeBytes)}</span>
              <span>{resolvedContentType || artifact.mimeType}</span>
            </div>
            <code class="artifact-path">{artifact.path}</code>
          </div>

          <div class="artifact-actions">
            {#if objectUrl}
              <button type="button" class="action-btn secondary" onclick={handleOpenRaw}>
                Open Raw
              </button>
            {/if}
            <button type="button" class="action-btn" onclick={handleDownload} disabled={!blobData}>
              Download
            </button>
          </div>
        </div>

        {#if loading}
          <div class="artifact-state">
            <Icon name="loading" size={22} />
            <span>Loading preview…</span>
          </div>
        {:else if error}
          <div class="artifact-state error">
            <Icon name="error" size={22} />
            <span>{error}</span>
          </div>
        {:else if isImageType(resolvedContentType || artifact.mimeType) && objectUrl}
          <div class="image-viewer">
            <img src={objectUrl} alt={resolvedFilename || artifact.name} />
          </div>
        {:else if isPdfType(resolvedContentType || artifact.mimeType) && objectUrl}
          <iframe
            class="pdf-viewer"
            src={objectUrl}
            title={resolvedFilename || artifact.name}
          ></iframe>
        {:else if isTextLike(resolvedContentType || artifact.mimeType, resolvedFilename || artifact.name)}
          <pre class="text-viewer">{textContent}</pre>
        {:else}
          <div class="artifact-state">
            <Icon name="info" size={22} />
            <span>This file type does not have an inline preview yet. Use Download to inspect it.</span>
          </div>
        {/if}
      </div>
    {/if}
  {/snippet}
</Modal>

<style>
  .artifact-viewer {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: min(960px, 82vw);
    min-height: min(620px, 78vh);
  }

  .artifact-toolbar {
    display: flex;
    justify-content: space-between;
    gap: var(--spacing-md);
    align-items: flex-start;
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-md);
  }

  .artifact-meta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    min-width: 0;
  }

  .artifact-name-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    color: var(--text-primary);
  }

  .artifact-details {
    display: flex;
    gap: var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    flex-wrap: wrap;
  }

  .artifact-path {
    display: block;
    max-width: 100%;
    overflow-x: auto;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    background: var(--bg-elevated-2);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
  }

  .artifact-actions {
    display: flex;
    gap: var(--spacing-sm);
    flex-shrink: 0;
  }

  .action-btn {
    border: 1px solid var(--accent-primary);
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border-radius: var(--radius-sm);
    padding: 0.65rem 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity var(--transition-fast), transform var(--transition-fast);
  }

  .action-btn.secondary {
    background: transparent;
    color: var(--text-primary);
    border-color: var(--border-default);
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn:not(:disabled):hover {
    opacity: 0.9;
    transform: translateY(-1px);
  }

  .artifact-state {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    min-height: 420px;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--bg-elevated) 75%, transparent);
    text-align: center;
    padding: var(--spacing-lg);
  }

  .artifact-state.error {
    color: var(--error);
  }

  .image-viewer {
    min-height: 420px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: radial-gradient(circle at top, color-mix(in srgb, var(--accent-primary) 12%, transparent), transparent 55%), var(--bg-elevated-2);
    border-radius: var(--radius-md);
    overflow: auto;
    padding: var(--spacing-lg);
  }

  .image-viewer img {
    max-width: 100%;
    max-height: 68vh;
    object-fit: contain;
    border-radius: var(--radius-sm);
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.22);
  }

  .pdf-viewer {
    width: 100%;
    min-height: 68vh;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: white;
  }

  .text-viewer {
    margin: 0;
    min-height: 420px;
    max-height: 68vh;
    overflow: auto;
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-primary);
  }

  @media (max-width: 900px) {
    .artifact-viewer {
      min-width: auto;
      min-height: auto;
    }

    .artifact-toolbar {
      flex-direction: column;
    }

    .artifact-actions {
      width: 100%;
      justify-content: flex-end;
    }
  }
</style>
