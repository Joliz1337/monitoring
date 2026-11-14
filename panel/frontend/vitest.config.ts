import { defineConfig } from 'vitest/config'

// Отдельно от vite.config.ts: сборка образа (`tsc && vite build`) этот файл не читает.
// Тесты лежат в tests/, вне src, — их типы не проверяет tsc приложения
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
    restoreMocks: true,
    // Тесты страниц рисуют сотни строк в jsdom: на раннере CI с двумя медленными
    // ядрами это занимает до 15 с, и пять тестов падали по умолчательным 5 с
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
})
