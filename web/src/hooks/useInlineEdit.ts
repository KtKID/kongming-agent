import { useState, useCallback, type ChangeEvent, type KeyboardEvent } from "react";

export interface UseInlineEditOptions {
  onConfirm: (newValue: string) => Promise<void>;
  onCancel?: () => void;
}

export interface UseInlineEditReturn {
  editing: boolean;
  editValue: string;
  setEditValue: (v: string) => void;
  startEdit: (initialValue: string) => void;
  cancelEdit: () => void;
  inputProps: {
    autoFocus: true;
    value: string;
    onChange: (e: ChangeEvent<HTMLInputElement>) => void;
    onBlur: () => void;
    onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
  };
}

/**
 * 内联编辑 hook：管理 editing 状态 + Enter/Escape/blur 处理。
 *
 * 调用方只需把 `inputProps` 展开到 `<input>` 上，
 * 提交逻辑走 `onConfirm` 回调（async，toast 由调用方处理）。
 */
export function useInlineEdit({
  onConfirm,
  onCancel,
}: UseInlineEditOptions): UseInlineEditReturn {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState("");

  const startEdit = useCallback((initialValue: string) => {
    setEditValue(initialValue);
    setEditing(true);
  }, []);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    onCancel?.();
  }, [onCancel]);

  const confirm = useCallback(async () => {
    const trimmed = editValue.trim();
    if (!trimmed) {
      setEditing(false);
      return;
    }
    await onConfirm(trimmed);
    setEditing(false);
  }, [editValue, onConfirm]);

  const inputProps = {
    autoFocus: true as const,
    value: editValue,
    onChange: (e: ChangeEvent<HTMLInputElement>) => setEditValue(e.target.value),
    onBlur: () => void confirm(),
    onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") void confirm();
      if (e.key === "Escape") cancelEdit();
    },
  };

  return { editing, editValue, setEditValue, startEdit, cancelEdit, inputProps };
}
