import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const proxy = {
    "/api": {
      target: env.BACKEND_URL || "http://127.0.0.1:8000",
      // Preserve the frontend Host so Django's same-origin CSRF checks also
      // work for requests forwarded by the development/preview proxy.
      changeOrigin: false,
    },
  };
  return {
    server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
    build: { outDir: "dist", emptyOutDir: true },
  };
});
