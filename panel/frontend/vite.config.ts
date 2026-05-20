import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    minify: 'esbuild',
    sourcemap: false,
    cssCodeSplit: true,
    cssMinify: true,
    assetsInlineLimit: 8192,
    chunkSizeWarningLimit: 800,
    reportCompressedSize: false,
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'chart-vendor': ['apexcharts', 'react-apexcharts'],
          'ui-vendor': ['framer-motion', 'lucide-react'],
          'i18n-vendor': ['i18next', 'react-i18next'],
          'dnd-vendor': ['@dnd-kit/core', '@dnd-kit/sortable', '@dnd-kit/utilities'],
        },
      },
    },
  },
  esbuild: {
    legalComments: 'none',
    drop: mode === 'production' ? ['console', 'debugger'] : [],
  },
}))
