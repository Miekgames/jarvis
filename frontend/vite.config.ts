import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const BACKEND = process.env.JARVIS_BACKEND ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      // Same-origin in dev, so cookies and the websocket behave exactly as
      // they will in a built single-process deployment.
      '/api': { target: BACKEND, changeOrigin: true, ws: true },
    },
  },
  preview: { port: 3000, strictPort: true },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
