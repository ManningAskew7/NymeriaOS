<script lang="ts">
  interface Props {
    size?: 'sm' | 'md' | 'lg';
  }

  let { size = 'md' }: Props = $props();
</script>

<span class="spinner spinner-{size}"></span>

<style>
  .spinner {
    display: inline-block;
    border: 2px solid var(--border-subtle);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  .spinner-sm {
    width: 14px;
    height: 14px;
  }

  .spinner-md {
    width: 20px;
    height: 20px;
  }

  .spinner-lg {
    width: 28px;
    height: 28px;
    border-width: 3px;
  }
</style>
