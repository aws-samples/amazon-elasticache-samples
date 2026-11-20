// Build, dev server and tests for the control-plane UI.
//
// Vite and Vitest replaced Create React App (react-scripts 5.0.1), which is no
// longer maintained and pinned build tools with known vulnerabilities (webpack
// dev server, svgo, jest 27, serialize-javascript ...) that no CRA release fixes.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The REACT_APP_* settings src/apiUrl.js reads, fixed at build time as before.
// Only these names are exposed to the bundle, never the whole environment.
const env = {};
for (const name of ['REACT_APP_API_URL', 'REACT_APP_API_PORT', 'REACT_APP_API_SAME_ORIGIN']) {
  env[`process.env.${name}`] = JSON.stringify(process.env[name] || '');
}

export default defineConfig({
  plugins: [react()],
  // The components are .js files with JSX, as Create React App allowed.
  esbuild: { loader: 'jsx', include: /src\/.*\.js$/, exclude: [] },
  optimizeDeps: { esbuildOptions: { loader: { '.js': 'jsx' } } },
  define: env,
  build: {
    // Same layout as before: nginx serves build/ and caches /static/ for a year.
    outDir: 'build',
    assetsDir: 'static',
    sourcemap: false,
  },
  // Development only (docker-compose.yml). Loopback unless the container asks
  // for 0.0.0.0 (frontend/Dockerfile), which compose publishes on 127.0.0.1.
  server: { port: 3000, strictPort: true, host: process.env.VITE_HOST || '127.0.0.1' },
  preview: { port: 3000, strictPort: true, host: '127.0.0.1' },
  test: {
    environment: 'jsdom',
    globals: true,
    css: false,
    include: ['src/**/*.test.js'],
  },
});
