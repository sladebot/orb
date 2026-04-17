import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['web/tests/unit/**/*.test.js'],
    environment: 'node',
    reporters: 'default',
    globals: false,
  },
});
