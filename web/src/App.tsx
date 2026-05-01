import { RouterProvider } from "react-router-dom";
import { router } from "@/lib/router";

/** 顶层 App：仅装载路由。全局 Provider 在 main.tsx。 */
export default function App() {
  return <RouterProvider router={router} />;
}
