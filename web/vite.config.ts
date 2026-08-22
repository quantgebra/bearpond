import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // Ship the production build inside the Python package so FastAPI can serve it.
    outDir: '../src/bearpond/server/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // During development, API calls from the React app are forwarded to the FastAPI backend.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ui': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
