import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + tile requests to the FastAPI backend so the frontend
// can use relative URLs (e.g. the tile_url templates returned by the API).
const API = process.env.TUNDRA_API_URL || "http://localhost:8000";

// Base public path. In dev this stays "/" (paths proxied at root); for a
// sub-path deployment (e.g. behind Apache at /tundra/) set TUNDRA_BASE=/tundra/.
const BASE = process.env.TUNDRA_BASE || "/";

export default defineConfig({
  base: BASE,
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: Object.fromEntries(
      ["/health", "/footprints", "/roi", "/segment", "/jobs", "/tiles"].map((p) => [
        p,
        { target: API, changeOrigin: true },
      ]),
    ),
  },
});
