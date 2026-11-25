import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative asset paths so the built site works from any static path on
// Vercel (project root or a sub-path) without extra config.
export default defineConfig({
  plugins: [react()],
  base: "./",
});
