import { defineConfig } from 'vitest/config'

// Отдельно от vite.config.ts: сборка образа (`tsc && vite build`) этот файл не читает.
// Тесты лежат в tests/, вне src, — их типы не проверяет tsc приложения
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
    restoreMocks: true,
  },
})
