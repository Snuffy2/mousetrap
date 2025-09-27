import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(() => {
  return {
    server: {
      // Vite's dev server defaults to 5173. Explicitly set it and
      // proxy API requests to the backend uvicorn server so the
      // frontend can use relative paths like /api/... in development.
      port: 5173,
      proxy: {
        // Proxy API calls to the backend running on PORT (default 39842)
        '/api': {
          target: 'http://localhost:39842',
          changeOrigin: true,
          secure: false,
        },
        // Proxy logs (served by backend) so fetch('/logs/...') works in dev
        '/logs': {
          target: 'http://localhost:39842',
          changeOrigin: true,
          secure: false,
        },
      },
    },
    build: {
      outDir: 'build',
    },
    plugins: [react()],
  };
});
