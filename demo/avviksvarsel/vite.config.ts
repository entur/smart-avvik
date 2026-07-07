import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /varsel (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as demo/reisedemo. public/ (manifest, service worker, icons) is
// copied to the output root, i.e. served at /varsel/sw.js, /varsel/manifest.webmanifest, etc.
export default defineConfig({
  plugins: [react()],
  base: '/varsel/',
  build: {
    outDir: '../../disruptions/api/varsel_static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Local dev: `uvicorn main:app` in disruptions/api serves the data + push endpoints.
      '/varsel-api': 'http://localhost:8000',
    },
  },
});
