import { defineConfig } from 'vite';
import { resolve } from 'node:path';

/**
 * Three entry points:
 *  - `index.html` — the patient client, driven by the SSE stream from the gateway.
 *  - `demo.html`  — every one of the eight components rendered from a static
 *                   `SurfaceUpdate` fixture, so the surface can be seen with no
 *                   backend running.
 *  - `admin.html` — §12's nine admin pages, reading `/v1/admin/*`.
 *
 * Separate documents rather than routes in one app: they are separate audiences,
 * and the patient page has no use for the admin bundle (nor the other way round).
 * The admin's own navigation is by hash inside its document — see
 * `src/admin/router.ts` for why.
 *
 * The gateway URL is injected at build time via `VITE_GATEWAY_URL`; both clients
 * read it the same way and fall back to the same-origin `/v1` prefix.
 */
export default defineConfig({
  build: {
    target: 'es2022',
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        demo: resolve(__dirname, 'demo.html'),
        admin: resolve(__dirname, 'admin.html'),
      },
    },
  },
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      '/v1': { target: process.env['GATEWAY_PROXY_TARGET'] ?? 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
  preview: {
    port: 3000,
    strictPort: true,
  },
});
