import { useEffect } from "react";
import { RouterProvider } from "react-router-dom";
import { router } from "@/lib/router";
import { bindViewportHeightVar } from "@/lib/viewport";

export default function App() {
  useEffect(() => bindViewportHeightVar(), []);

  return <RouterProvider router={router} unstable_useTransitions={false} />;
}
