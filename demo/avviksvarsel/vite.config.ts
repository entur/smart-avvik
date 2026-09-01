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
    // Two entry documents, i.e. a Vite MPA. The privacy page is a SECOND HTML ENTRY rather than a
    // client route because the app has no router and the API's static mount has no SPA fallback —
    // a client route would 404 server-side on a direct hit, which is exactly how a privacy page is
    // reached (a link in a push, a pasted URL). Emitting personvern/index.html instead lets
    // Starlette's html=True serve it at /varsel/personvern/ with no backend change at all; the
    // Dockerfile copies varsel_static/ wholesale, so nothing else has to know it exists.
    rollupOptions: {
      input: {
        main: 'index.html',
        personvern: 'personvern/index.html',
      },
    },
  },
  server: {
    proxy: {
      // Local dev: `uvicorn main:app` in disruptions/api serves the data + push endpoints.
      '/varsel-api': 'http://localhost:8000',
    },
  },
});
