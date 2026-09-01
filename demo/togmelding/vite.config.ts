import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /togmelding (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as demo/avviksvarsel and demo/varselhistorikk. Note the push
// notifications that carry travellers here link to `/togmelding` without a trailing slash, which
// the mount answers with this app's index.html.
export default defineConfig({
  plugins: [react()],
  base: '/togmelding/',
  build: {
    outDir: '../../disruptions/api/togmelding_static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Local dev: `uvicorn main:app` in disruptions/api serves the forecast endpoints.
      '/togmelding-api': 'http://localhost:8000',
      // The feedback widget posts to the SHARED /varsel-api endpoint (see api.ts).
      '/varsel-api': 'http://localhost:8000',
    },
  },
});
