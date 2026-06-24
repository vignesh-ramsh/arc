import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],

  // Must match the Starlette StaticFiles mount point in plugin.py
  base: '/admin/',

  build: {
    // Output goes directly into the plugin's ui/ directory — committed to git
    outDir: '../ui',
    emptyOutDir: true,

    rollupOptions: {
      output: {
        // Stable chunk names so git diffs are readable
        manualChunks: {
          vendor: ['react', 'react-dom'],
        },
      },
    },
  },

  server: {
    // Dev: proxy all /api/ and /admin/api/ calls to Arc
    // Usage: VITE_ARC_URL=http://localhost:8000 npm run dev
    proxy: {
      '/api': {
        target: process.env.VITE_ARC_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
