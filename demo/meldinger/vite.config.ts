import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /meldinger (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as demo/reisedemo, demo/avviksvarsel and demo/varselhistorikk.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'MELDINGER_');
  return {
    plugins: [react()],
    base: '/meldinger/',
    build: {
      outDir: '../../disruptions/api/meldinger_static',
      emptyOutDir: true,
    },
    server: {
      proxy: {
        // Local dev: `uvicorn main:app` in disruptions/api serves the replay endpoints.
        '/meldinger-api': env.MELDINGER_API_URL || 'http://localhost:8000',
      },
    },
  };
});
