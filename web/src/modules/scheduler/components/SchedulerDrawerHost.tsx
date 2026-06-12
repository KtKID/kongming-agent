/**
 * 抽屉宿主——负责 open state 与 host-level wiring
 *
 * v0.5.3：dialog 状态从 store 提到本地 useState，支持三种 mode
 * （create / edit / duplicate），并把 onEdit / onDuplicate 回调
 * 通过 props 下钻到 SchedulerDrawer → SchedulerTaskList → Row。
 */

import { useState, useCallback } from "react";
import { SchedulerDrawer } from "./SchedulerDrawer";
import {
  CreateSchedulerTaskDialog,
  type DialogMode,
} from "./CreateSchedulerTaskDialog";
import { useSchedulerRealtime } from "../hooks/useSchedulerRealtime";
import type { SchedulerTaskVM } from "../types";

interface DialogState {
  open: boolean;
  mode: DialogMode;
  initialTask?: SchedulerTaskVM;
}

export function SchedulerDrawerHost() {
  useSchedulerRealtime();

  const [dialogState, setDialogState] = useState<DialogState>({
    open: false,
    mode: "create",
  });

  const handleOpenCreate = useCallback(() => {
    setDialogState({ open: true, mode: "create", initialTask: undefined });
  }, []);

  const handleEditTask = useCallback((task: SchedulerTaskVM) => {
    setDialogState({ open: true, mode: "edit", initialTask: task });
  }, []);

  const handleDuplicateTask = useCallback((task: SchedulerTaskVM) => {
    setDialogState({ open: true, mode: "duplicate", initialTask: task });
  }, []);

  const handleClose = useCallback(() => {
    setDialogState((prev) => ({ ...prev, open: false }));
  }, []);

  return (
    <>
      <SchedulerDrawer
        onOpenCreate={handleOpenCreate}
        onEditTask={handleEditTask}
        onDuplicateTask={handleDuplicateTask}
      />
      <CreateSchedulerTaskDialog
        open={dialogState.open}
        mode={dialogState.mode}
        initialTask={dialogState.initialTask}
        onClose={handleClose}
      />
    </>
  );
}
