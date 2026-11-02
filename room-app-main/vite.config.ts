import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ command }) => ({
  server: {
    proxy: {
      '/api': {
        target: 'https://cosai.nrru.ac.th:8000',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  plugins: [
    {
      name: 'production-config',
      enforce: 'pre',
      resolveId(source) {
        if (command === 'build' && source.endsWith('/assets/config.json')) {
          return fileURLToPath(new URL('./src/assets/config.production.json', import.meta.url))
        }
      },
    },
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icons/icon-192.svg', 'icons/icon-512.svg'],
      manifest: {
        name: 'ระบบบริหารจัดการห้องคอมพิวเตอร์',
        short_name: 'ระบบห้องคอมพิวเตอร์',
        description: 'ระบบจองห้องและแสดงตารางการใช้ห้องคอมพิวเตอร์',
        theme_color: '#0f172a',
        background_color: '#0f172a',
        display: 'standalone',
        icons: [
          {
            src: '/icons/icon-192.svg',
            sizes: '192x192',
            type: 'image/svg+xml',
          },
          {
            src: '/icons/icon-512.svg',
            sizes: '512x512',
            type: 'image/svg+xml',
          },
        ],
      },
    }),
  ],
}))
