import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";

export function NotFoundPage() {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-background p-4">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.24, ease: [0, 0, 0.2, 1] }}
        className="flex flex-col items-center gap-4"
      >
        <div className="text-5xl font-bold tracking-tight">404</div>
        <div className="text-sm text-muted-foreground">页面不存在</div>
        <Button asChild>
          <Link to="/chat">返回对话</Link>
        </Button>
      </motion.div>
    </div>
  );
}
