import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

const apiProxyPaths = [
  "/tasks",
  "/runs",
  "/schedules",
  "/stats",
  "/telemetry",
  "/planner",
  "/watchers",
  "/profile",
  "/health",
  "/ready",
  "/metrics",
  "/openapi.json",
  "/docs",
  "/redoc"
];

export default defineConfig(({ command }) => {
  const proxy = Object.fromEntries(
    apiProxyPaths.map((prefix) => [
      prefix,
      {
        target: "http://localhost:8000",
        changeOrigin: true
      }
    ])
  );

  return {
    base: command === "build" ? "/app/" : "/",
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src")
      }
    },
    server: {
      port: 5173,
      proxy
    },
    preview: {
      port: 4173
    }
  };
});
