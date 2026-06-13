import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// kongming-agent web v0.1.5 vite 配置
//
// 关键决策：
// - alias `@` → `src/`，与 tsconfig paths 同步
// - dev proxy `/api` `/ws` → `localhost:8080`（后端 web-app-shell 端口）
// - manualChunks 拆 vendor / app，便于 gzip < 250KB 控制
// - sourcemap=false（产物体积优先；联调阶段需要时再开）
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/ws": {
        target: "ws://localhost:8080",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    minify: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router-dom"],
          // mermaid 不显式切：lazy(MermaidBlock) 边界让 vite 自动把
          // mermaid 库切到异步 chunk，无需 manualChunks 干预
        },
      },
    },
  },
});
