import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Served by the disruptions API at /rushhistorikk (see disruptions/api/main.py), so assets resolve
// relative to that prefix. Build output goes straight into the API's static directory — the same
// generated-directory pattern as the other five front-ends. `base` MUST match the mount path or
// every asset 404s.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'RUSHHISTORIKK_');
  return {
    plugins: [react()],
    base: '/rushhistorikk/',
    build: {
      outDir: '../../disruptions/api/rushhistorikk_static',
      emptyOutDir: true,
    },
    server: {
      proxy: {
        // Local dev. The target is overridable (RUSHHISTORIKK_API_URL) the way /meldinger's is,
        // and here that is more than a nicety: the practical way to look at one of these pages is a
        // stub on :8000, and several of them exist — so :8000 is regularly already taken by a
        // sibling app's stub or a real uvicorn, and a hardcoded target just 404s the page rather
        // than saying so.
        //
        // Proxy EVERY prefix the page calls, not only the obvious one. /varsel proxied just
        // /varsel-api while its rush panel called /togmelding-api, and the panel 404'd under
        // `npm run dev` looking exactly like a server with nothing to say. This page calls one.
        '/rushhistorikk-api': env.RUSHHISTORIKK_API_URL || 'http://localhost:8000',
      },
    },
  };
});
