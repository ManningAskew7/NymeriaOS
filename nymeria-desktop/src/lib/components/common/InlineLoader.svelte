<script lang="ts">
  import Spinner from './Spinner.svelte';

  interface Props {
    /** Visible loading message; also the screen-reader announcement. */
    text?: string;
    size?: 'sm' | 'md' | 'lg';
  }

  let { text = 'Loading…', size = 'sm' }: Props = $props();
</script>

<!-- Inline-flex so it flows like the bare text it replaces: a centered parent
     (text-align: center) centers it, a left-aligned parent leaves it left, and
     it inherits the parent's text color and size. role="status" announces the
     message to screen readers when it appears. -->
<span class="inline-loader" role="status">
  <Spinner {size} />
  {#if text}<span>{text}</span>{/if}
</span>

<style>
  .inline-loader {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-sm);
  }
</style>
