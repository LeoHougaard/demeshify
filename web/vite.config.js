import { readFileSync } from 'node:fs';
import { defineConfig } from "vite";

export default defineConfig(({ mode }) => ({
  worker: { format: 'es' },
  define: {
    'import.meta.env.VITE_BROWSER_LOCAL': JSON.stringify(String(mode === 'browser')),
  },
  plugins: mode === 'browser' ? [{
    name: 'browser-license',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'LICENSE', source: readFileSync(new URL('../LICENSE', import.meta.url)) });
    },
  }] : [],
  build: {
    outDir: mode === 'browser' ? 'dist-browser' : 'dist',
    emptyOutDir: true,
  },
}));

