import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.ts'],
    // Patches jsdom's missing constructable-stylesheet API, which the A2UI basic
    // catalog's stylesheet injection needs. See the file for the reasoning.
    setupFiles: ['./test/setup.ts'],
    globals: false,
    restoreMocks: true,
  },
});
