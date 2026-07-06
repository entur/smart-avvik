import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /demo (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory —
// the same generated-directory pattern as eval_static/.
export default defineConfig({
  plugins: [react()],
  base: '/demo/',
  build: {
    outDir: '../../disruptions/api/demo_static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Local dev: `uvicorn main:app` in disruptions/api serves the data endpoints.
      '/demo-api': 'http://localhost:8000',
    },
  },
});
