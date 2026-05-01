import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useThreadsStore } from "@/stores/threads";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 新建 thread dialog：name + preset 选择 → POST /api/threads → 跳转 /chat/<id>
 *
 * preset 用 native <select>（v0.1.5 简化；shadcn select 后续按需引入）。
 */
export function NewThreadDialog({ open, onOpenChange }: Props) {
  const presets = useThreadsStore((s) => s.presets);
  const createThread = useThreadsStore((s) => s.createThread);
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [presetId, setPresetId] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setPresetId(presets[0]?.id ?? "");
    }
  }, [open, presets]);

  const onSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed || !presetId || busy) return;
    setBusy(true);
    try {
      const t = await createThread(trimmed, presetId);
      onOpenChange(false);
      navigate(`/chat/${t.id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`创建失败：${msg}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建会话</DialogTitle>
          <DialogDescription>选择 preset 并命名一个新会话</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Input
            placeholder="会话名（最多 200 字）"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            maxLength={200}
          />
          <select
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={presetId}
            onChange={(e) => setPresetId(e.target.value)}
            aria-label="preset"
          >
            {presets.length === 0 ? (
              <option value="">（暂无 preset）</option>
            ) : null}
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name} · {p.model}
              </option>
            ))}
          </select>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            onClick={() => void onSubmit()}
            disabled={busy || !name.trim() || !presetId}
          >
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
