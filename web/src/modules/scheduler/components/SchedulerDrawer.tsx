/**
 * 右侧抽屉壳层
 *
 * v0.5.3：dialog 状态（create / edit / duplicate）上提到 SchedulerDrawerHost；
 * 这里只接收回调（点 + 触发 create、点行 ✏️ / 📋 触发 edit/duplicate）。
 */

import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useSchedulerStore } from "../store";
import type { SchedulerTaskVM } from "../types";
import { SchedulerTaskList } from "./SchedulerTaskList";
import { SchedulerTaskDetail } from "./SchedulerTaskDetail";

interface Props {
  onOpenCreate: () => void;
  onEditTask: (task: SchedulerTaskVM) => void;
  onDuplicateTask: (task: SchedulerTaskVM) => void;
}

export function SchedulerDrawer({
  onOpenCreate,
  onEditTask,
  onDuplicateTask,
}: Props) {
  const isOpen = useSchedulerStore((s) => s.isDrawerOpen);
  const closeDrawer = useSchedulerStore((s) => s.closeDrawer);
  const errorMessage = useSchedulerStore((s) => s.errorMessage);

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && closeDrawer()}>
      <SheetContent side="right" className="w-[420px] sm:max-w-[420px] flex flex-col p-0">
        <SheetHeader className="px-4 pt-4 pb-2">
          <SheetTitle>定时任务</SheetTitle>
        </SheetHeader>

        {errorMessage && (
          <div className="mx-4 mb-2 rounded-md bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
            {errorMessage}
          </div>
        )}

        {/* 列表 + 详情 */}
        <div className="flex-1 overflow-y-auto px-4 pb-2">
          <SchedulerTaskList
            onEditTask={onEditTask}
            onDuplicateTask={onDuplicateTask}
          />
          <SchedulerTaskDetail />
        </div>

        {/* 底部创建按钮 */}
        <div className="border-t border-border px-4 py-3">
          <Button
            className="w-full"
            size="sm"
            onClick={onOpenCreate}
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            创建定时任务
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
