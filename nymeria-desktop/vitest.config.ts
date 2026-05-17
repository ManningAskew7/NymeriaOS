import { defineConfig } from 'vitest/config';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';

export default defineConfig({
  plugins: [
    svelte({
      hot: false,
      preprocess: vitePreprocess(),
      compilerOptions: { hmr: false },
    }),
  ],
  resolve: {
    alias: {
      $lib: path.resolve(__dirname, 'src/lib'),
    },
    conditions: ['browser'],
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
