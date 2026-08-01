import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /historikk (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as demo/reisedemo and demo/avviksvarsel.
export default defineConfig({
  plugins: [react()],
  base: '/historikk/',
  build: {
    outDir: '../../disruptions/api/historikk_static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Local dev: `uvicorn main:app` in disruptions/api serves the decision-log endpoints.
      '/historikk-api': 'http://localhost:8000',
    },
  },
});
