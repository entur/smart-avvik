import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /tilbakeblikk (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as demo/reisedemo, demo/avviksvarsel and demo/varselhistorikk.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'TILBAKEBLIKK_');
  return {
    plugins: [react()],
    base: '/tilbakeblikk/',
    build: {
      outDir: '../../disruptions/api/tilbakeblikk_static',
      emptyOutDir: true,
    },
    server: {
      proxy: {
        // Local dev: `uvicorn main:app` in disruptions/api serves the replay endpoints.
        '/tilbakeblikk-api': env.TILBAKEBLIKK_API_URL || 'http://localhost:8000',
      },
    },
  };
});
