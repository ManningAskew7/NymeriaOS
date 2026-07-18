<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type { ServerSettings, ServerSettingsUpdate } from '$lib/types';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    settings: ServerSettings | null;
    refresh: () => Promise<void>;
  }

  let { settings, refresh }: Props = $props();

  // The same capability backends the CLI wizard's backend_keys step collects.
  // All write-only: a stored key is never echoed back, so blank means "keep".
  const GROUPS: {
    title: string;
    fields: { key: keyof ServerSettingsUpdate; label: string; hint: string }[];
  }[] = [
    {
      title: 'Web search',
      fields: [
        { key: 'tavily_api_key', label: 'Tavily', hint: 'web_search_tavily' },
        { key: 'exa_api_key', label: 'Exa', hint: 'web_search_exa_ai' },
        { key: 'brave_api_key', label: 'Brave Search', hint: 'web_search_brave' },
        { key: 'perplexity_api_key', label: 'Perplexity', hint: 'web_search_perplexity' },
      ],
    },
    {
      title: 'Fetch and crawl',
      fields: [
        { key: 'firecrawl_api_key', label: 'Firecrawl', hint: 'web_search_firecrawl and fetch' },
        { key: 'jina_api_key', label: 'Jina AI', hint: 'URL reader fallback' },
      ],
    },
    {
      title: 'Image generation',
      fields: [
        { key: 'bfl_api_key', label: 'Black Forest Labs', hint: 'image_gen_flux' },
        { key: 'replicate_api_key', label: 'Replicate', hint: 'image_gen_replicate' },
        { key: 'fal_api_key', label: 'fal.ai', hint: 'image_gen_fal' },
      ],
    },
  ];

  let values = $state<Record<string, string>>({});
  let searxngBaseUrl = $state('');
  let saveStatus = $state<'idle' | 'saving' | 'success' | 'error'>('idle');
  let saveMessage = $state('');

  const hasInput = $derived(
    Object.values(values).some((v) => v.trim()) || searxngBaseUrl.trim().length > 0
  );

  async function handleSave() {
    if (!hasInput) return;
    saveStatus = 'saving';
    saveMessage = '';
    try {
      const updates: ServerSettingsUpdate = {};
      for (const [key, value] of Object.entries(values)) {
        if (value.trim()) {
          (updates as Record<string, unknown>)[key] = value.trim();
        }
      }
      if (searxngBaseUrl.trim()) updates.searxng_base_url = searxngBaseUrl.trim();
      await api.updateServerSettings(updates);
      saveStatus = 'success';
      saveMessage = 'Keys saved. The matching tools light up on the next turn.';
      values = {};
      searxngBaseUrl = '';
      await refresh();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the integration keys' });
    }
  }
</script>

<p class="sf-hint lead">
  Optional keys for the built-in web search, fetch, and image tools. The keyless DuckDuckGo
  search works out of the box; everything here is an upgrade, not a requirement. Blank fields
  keep whatever is already stored.
</p>

{#each GROUPS as group (group.title)}
  <div class="sf-field">
    <span class="sf-label">{group.title}</span>
    <div class="sf-grid two">
      {#each group.fields as field (field.key)}
        <label class="sf-sub">
          <span>{field.label} <em class="tool-ref">{field.hint}</em></span>
          <input
            class="sf-input"
            type="password"
            value={values[field.key] ?? ''}
            oninput={(e) => (values = { ...values, [field.key]: e.currentTarget.value })}
            placeholder="API key"
            autocomplete="off"
          />
        </label>
      {/each}
    </div>
  </div>
{/each}

<div class="sf-field">
  <label class="sf-label" for="setup-searxng">SearXNG base URL</label>
  <input
    id="setup-searxng"
    class="sf-input"
    type="text"
    bind:value={searxngBaseUrl}
    placeholder="https://searx.example.org"
  />
  <p class="sf-hint">Self-hosted keyless meta-search, if you run one.</p>
</div>

<div class="sf-actions">
  <Button
    variant="primary"
    onclick={handleSave}
    disabled={!hasInput || saveStatus === 'saving' || !settings}
    loading={saveStatus === 'saving'}
  >
    {saveStatus === 'saving' ? 'Saving' : 'Save keys'}
  </Button>
</div>

{#if saveMessage}
  <div class="sf-result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
    <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
    <span>{saveMessage}</span>
  </div>
{/if}

<style>
  .lead {
    margin: 0;
  }

  .tool-ref {
    font-style: normal;
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }
</style>
