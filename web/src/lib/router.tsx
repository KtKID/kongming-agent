import { createBrowserRouter, Navigate } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { AuthGuard } from "@/components/AuthGuard";
import { LoginPage } from "@/pages/Login";
import { ChatPage } from "@/pages/Chat";
import { ManagePage } from "@/pages/Manage";
import { NotFoundPage } from "@/pages/NotFound";
import { RuntimeStatusPage } from "@/modules/dashboard";
import { ConfigPage } from "@/modules/dashboard/config";
import { ModelProvidersPage } from "@/modules/model-providers";
import { PluginsPage } from "@/modules/plugin-management";
import { TaskDetailOverlayPage } from "@/modules/task-detail";

/**
 * v0.1.5 路由表（manage-config-tab #19 后）：
 * - /login                          公开
 * - /(任何其它) → AuthGuard 包裹 → Layout → 子路由
 * - /                                → /chat（默认重定向）
 * - /chat[/:thread_id]
 * - /manage                          → /manage/config（默认重定向）
 * - /manage/config                   → /manage/config/model（section 兜底）
 * - /manage/config/:section          ConfigPage（合法 section 由后端 schema.groups 决定）
 * - /manage/model-providers          ModelProvidersPage
 * - /manage/network                  RuntimeStatusPage（原 /manage/runtime-status，破坏性更名，无兼容 alias）
 * - 其它 → NotFound
 */
export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: <AuthGuard />,
    children: [
      {
        element: <Layout />,
        children: [
          { index: true, element: <Navigate to="/chat" replace /> },
          { path: "chat", element: <ChatPage /> },
          {
            path: "chat/:thread_id",
            element: <ChatPage />,
            children: [
              { path: "task-detail", element: <TaskDetailOverlayPage /> },
              {
                path: "task-detail/files/:artifact_id",
                element: <TaskDetailOverlayPage />,
              },
              {
                path: "agent-workflows",
                element: <TaskDetailOverlayPage />,
              },
              {
                path: "agent-workflows/:workflow_id",
                element: <TaskDetailOverlayPage />,
              },
            ],
          },
          {
            path: "manage",
            element: <ManagePage />,
            children: [
              { index: true, element: <Navigate to="config" replace /> },
              { path: "config", element: <Navigate to="model" replace /> },
              { path: "config/:section", element: <ConfigPage /> },
              { path: "plugins", element: <PluginsPage /> },
              { path: "model-providers", element: <ModelProvidersPage /> },
              { path: "network", element: <RuntimeStatusPage /> },
            ],
          },
        ],
      },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
]);
