import { RouterProvider } from "react-router-dom";
import { router } from "@/lib/router";

/** 顶层 App：仅装载路由。全局 Provider 在 main.tsx。 */
export default function App() {
  // unstable_useTransitions={false} 关掉 RouterProvider 默认的 startTransition
  // 包裹。React 19.2.5 + react-router-dom@7.14.2 下，transition 包裹的 setState
  // 不会 commit，导致 LocationContext.Provider 不下传新 location，子树的
  // useLocation/useParams/useMatches 全部冻在初始路由（thread 切换 URL 变 UI 不变）。
  return <RouterProvider router={router} unstable_useTransitions={false} />;
}
