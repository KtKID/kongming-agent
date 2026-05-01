import { createBrowserRouter, Navigate } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { AuthGuard } from "@/components/AuthGuard";
import { LoginPage } from "@/pages/Login";
import { ChatPage } from "@/pages/Chat";
import { ManagePage } from "@/pages/Manage";
import { NotFoundPage } from "@/pages/NotFound";

/**
 * v0.1.5 路由表：
 * - /login          公开
 * - /(任何其它) → AuthGuard 包裹 → Layout → 子路由
 * - /              → /chat（默认重定向）
 * - /chat[/:thread_id]
 * - /manage
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
          { path: "chat/:thread_id", element: <ChatPage /> },
          { path: "manage", element: <ManagePage /> },
        ],
      },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
]);
