import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import type { UserInputAttachment } from "@/protocol";

/**
 * 附件上传状态机：
 *   uploading → ready
 *   uploading → failed
 *
 * "failed" 既包含客户端预校验失败（mime/size），也包含服务端错误。
 * 不存在 uploading→uploading 这种状态变更。
 */
export type UploadStatus = "uploading" | "ready" | "failed";

/**
 * 单条附件的前端状态。
 *
 * `id` 是客户端临时 id（crypto.randomUUID）；uploading / failed 阶段用它做主键。
 * `attachment.asset_id` 仅在 status="ready" 时存在，作为发送给后端的真值。
 */
export interface AttachmentState {
  /** 客户端临时 id（uploading/failed 阶段使用；ready 后 attachment.asset_id 才是真正的服务端 id） */
  id: string;
  status: UploadStatus;
  filename: string;
  size_bytes: number;
  mime_type: string;
  /** 本地预览（FileReader → object URL 或 data URL）—— uploading 阶段就能显示 */
  local_preview_url: string;
  /** 服务端返回的资产引用，status="ready" 时有值 */
  attachment?: UserInputAttachment;
  error_code?: string;
  error_message?: string;
}

export interface UseAttachmentUploaderReturn {
  /** 当前所有附件（含 uploading / ready / failed） */
  attachments: AttachmentState[];
  /** 上传一个 File；返回客户端临时 id（便于上层关联） */
  upload: (file: File, threadId: string) => string;
  /** 用已上传成功的资产引用恢复 ready 状态列表，并同步附件数量计数器。 */
  restoreReadyAttachments: (attachments: UserInputAttachment[]) => void;
  /** 移除单个附件（不论状态）。ready 状态附件移除时**不**调后端删除，仅前端丢弃 */
  remove: (id: string) => void;
  /** 清空所有附件（发送后调用） */
  clear: () => void;
  /** 仅 ready 状态的附件投影，给 WS 帧用 */
  readyAttachments: UserInputAttachment[];
  /** 是否有正在上传的（用于发送按钮禁用） */
  hasUploading: boolean;
  /** 失败数（用于发送时校验） */
  failedCount: number;
}

const ALLOWED_MIME_TYPES = new Set<string>([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

const MAX_SIZE_BYTES = 5 * 1024 * 1024;

/**
 * 单条消息最多挂多少张图（DoD #9 / README §1 硬限制）。
 *
 * Anthropic Messages API 单 message 的 image content block 数量上限是 6，
 * 这里在前端硬阻拦，避免发出去被后端 4xx 拒绝才发现。
 *
 * export 出来供测试和上层（Composer）共享单点真源。
 */
export const MAX_ATTACHMENT_COUNT = 6;

const ERROR_TEXT: Record<string, string> = {
  mime_not_allowed: "不支持的图片格式（仅 png / jpeg / webp / gif）",
  too_large: "图片超过 5MB 限制",
  empty_payload: "图片为空",
  thread_not_found: "会话不存在或已删除",
  count_exceeded: `最多 ${MAX_ATTACHMENT_COUNT} 张图，请删除一张再添加`,
};

interface ServerErrorDetail {
  code?: string;
  message?: string;
}

/**
 * 解析后端错误响应。后端约定 4xx/5xx 返回
 *   { "detail": { "code": "...", "message": "..." } }
 * 或 FastAPI 默认的 { "detail": "..." }（字符串）。
 *
 * 返回 { code, message }，code 缺省为 "unknown"，message 缺省为 HTTP statusText。
 */
async function parseErrorResponse(
  resp: Response,
): Promise<{ code: string; message: string }> {
  let code = "unknown";
  let message = resp.statusText || "上传失败";
  try {
    const body = (await resp.json()) as { detail?: ServerErrorDetail | string };
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      if (typeof detail.code === "string") code = detail.code;
      if (typeof detail.message === "string") message = detail.message;
    } else if (typeof detail === "string") {
      message = detail;
    }
  } catch {
    // body 不是 JSON，沿用 statusText
  }
  return { code, message };
}

/**
 * 把 error code 映射到中文 toast 文案；未知 code 回落到 "上传失败：${message}"。
 */
function toastUploadError(code: string, message: string): void {
  const text = ERROR_TEXT[code] ?? `上传失败：${message}`;
  toast.error(text);
}

/**
 * 附件上传 hook：封装 client-side 预校验 + multipart 上传 + 状态机维护 +
 * object URL 资源回收。对外只暴露 8 个字段，所有 state/callback/memo 内聚在
 * hook 内。
 *
 * 未来 `+` 号按钮 / 拖拽上传共享同一 hook，只需在调用方触发 `upload(file, threadId)`。
 */
export function useAttachmentUploader(): UseAttachmentUploaderReturn {
  const [attachments, setAttachments] = useState<AttachmentState[]>([]);

  // 用 ref 持有当前已分配的 object URL 集合，确保 unmount 时能全部 revoke
  // 即便 state 在 unmount 后被清空（避免内存泄漏）。
  const objectUrlsRef = useRef<Set<string>>(new Set());

  // attachments 数量同步快照（DoD #9 计数器）：
  // 不能用 setAttachments(prev => ...) 的 updater 来读 prev——React 18 在
  // batched updates 模式下 updater 是 lazy 入队执行，同步循环里多次调
  // setState 时闭包变量看不到累积值。改用 ref 在每次成功增/删时同步更新，
  // upload 入口同步 +1 判断，超限即原子回退 -1，避免快速粘贴穿透 6 张上限。
  const countRef = useRef<number>(0);

  /**
   * 启动一次上传：
   * 1. 生成临时 id 和 local_preview_url
   * 2. 客户端预校验 mime / size，失败直接入列 failed + toast，不发请求
   * 3. 通过则插入 uploading 条目并异步 fetch；fetch 完成后切 ready 或 failed
   *
   * 返回临时 id，方便上层关联（如关联粘贴事件）。
   */
  const upload = useCallback((file: File, threadId: string): string => {
    const tempId = crypto.randomUUID();

    // ---- 数量校验（DoD #9 单消息 ≤ 6 张图，README §1 硬限制）----
    // 同步 +1 计数：countRef.current 是当前列表长度的实时镜像
    // （remove/clear 后会被同步减回）。超过 6 即原子回退并 toast，
    // 不调 setState 不污染列表。
    countRef.current += 1;
    if (countRef.current > MAX_ATTACHMENT_COUNT) {
      countRef.current -= 1;
      toastUploadError("count_exceeded", "");
      return tempId;
    }

    const previewUrl = URL.createObjectURL(file);
    objectUrlsRef.current.add(previewUrl);

    const base: Omit<AttachmentState, "status"> = {
      id: tempId,
      filename: file.name,
      size_bytes: file.size,
      mime_type: file.type,
      local_preview_url: previewUrl,
    };

    // ---- 客户端预校验 ----
    //
    // failed 入列前必须把 countRef -= 1。
    // countRef 在 upload 入口已 +1（占用 6 张额度），客户端校验失败的条目不应继续占额度——
    // 否则用户连粘 6 张 bmp 后正常 png 就会被 count_exceeded 误拒。
    // 语义变更：countRef 只算"ready + uploading"，failed 是临时状态不占额度。
    if (file.size === 0) {
      countRef.current -= 1;
      const code = "empty_payload";
      toastUploadError(code, "");
      setAttachments((prev) => [
        ...prev,
        {
          ...base,
          status: "failed",
          error_code: code,
          error_message: ERROR_TEXT[code] ?? "empty",
        },
      ]);
      return tempId;
    }
    if (!ALLOWED_MIME_TYPES.has(file.type)) {
      countRef.current -= 1;
      const code = "mime_not_allowed";
      toastUploadError(code, "");
      setAttachments((prev) => [
        ...prev,
        {
          ...base,
          status: "failed",
          error_code: code,
          error_message: ERROR_TEXT[code] ?? "mime not allowed",
        },
      ]);
      return tempId;
    }
    if (file.size > MAX_SIZE_BYTES) {
      countRef.current -= 1;
      const code = "too_large";
      toastUploadError(code, "");
      setAttachments((prev) => [
        ...prev,
        {
          ...base,
          status: "failed",
          error_code: code,
          error_message: ERROR_TEXT[code] ?? "file too large",
        },
      ]);
      return tempId;
    }

    // ---- 进入 uploading ----
    setAttachments((prev) => [...prev, { ...base, status: "uploading" }]);

    // 异步发请求，不 await（hook 是同步返回临时 id）
    void (async () => {
      try {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("thread_id", threadId);
        const resp = await fetch("/api/uploads/images", {
          method: "POST",
          body: fd,
          credentials: "include",
          // CSRFMiddleware 强制 mutating 方法带 X-Requested-With（仓库
          // src/web/auth.py:CSRFMiddleware）。fetch 不走 axios interceptor，
          // 必须显式加；否则 403 "CSRF guard: X-Requested-With required"。
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });

        if (!resp.ok) {
          const { code, message } = await parseErrorResponse(resp);
          toastUploadError(code, message);
          // uploading → failed：归还 countRef 槽位（与客户端预校验失败语义一致）。
          // 防御性 -1：仅在条目存在且仍为 uploading 时归还，避免重复转 failed 时多减。
          setAttachments((prev) => {
            const target = prev.find((a) => a.id === tempId);
            if (target && target.status === "uploading") {
              countRef.current = Math.max(0, countRef.current - 1);
            }
            return prev.map((a) =>
              a.id === tempId
                ? {
                    ...a,
                    status: "failed",
                    error_code: code,
                    error_message: message,
                  }
                : a,
            );
          });
          return;
        }

        const attachment = (await resp.json()) as UserInputAttachment;
        setAttachments((prev) =>
          prev.map((a) =>
            a.id === tempId
              ? {
                  ...a,
                  status: "ready",
                  attachment,
                }
              : a,
          ),
        );
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        toastUploadError("network_error", message);
        // 同上：网络错误也归还 countRef 槽位
        setAttachments((prev) => {
          const target = prev.find((a) => a.id === tempId);
          if (target && target.status === "uploading") {
            countRef.current = Math.max(0, countRef.current - 1);
          }
          return prev.map((a) =>
            a.id === tempId
              ? {
                  ...a,
                  status: "failed",
                  error_code: "network_error",
                  error_message: message,
                }
              : a,
          );
        });
      }
    })();

    return tempId;
  }, []);

  /**
   * 从服务端资产引用恢复 ready 附件列表。
   *
   * 输入是 Composer 已提交过的 UserInputAttachment 真源；输出是新的
   * AttachmentState[]，全部处于 ready 状态。恢复前释放旧 object URL，恢复后
   * countRef 与 readyAttachments.length 保持一致。
   */
  const restoreReadyAttachments = useCallback(
    (readyAttachments: UserInputAttachment[]): void => {
      setAttachments((prev) => {
        for (const a of prev) {
          if (objectUrlsRef.current.has(a.local_preview_url)) {
            URL.revokeObjectURL(a.local_preview_url);
            objectUrlsRef.current.delete(a.local_preview_url);
          }
        }
        return readyAttachments.map((attachment, index) => ({
          id: `${attachment.asset_id}:${index}`,
          status: "ready",
          filename: attachment.asset_id,
          size_bytes: attachment.size_bytes,
          mime_type: attachment.mime_type,
          local_preview_url: attachment.preview_url,
          attachment,
        }));
      });
      countRef.current = readyAttachments.length;
    },
    [],
  );

  /**
   * 删除单条附件：从列表移除 + 释放对应 object URL。
   * 不管状态如何（uploading 中的也允许删，正在飞的请求结果到达时会发现条目已消失，
   * setAttachments 的 map 会自动跳过）。
   */
  const remove = useCallback((id: string): void => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target && objectUrlsRef.current.has(target.local_preview_url)) {
        URL.revokeObjectURL(target.local_preview_url);
        objectUrlsRef.current.delete(target.local_preview_url);
      }
      const next = prev.filter((a) => a.id !== id);
      // 同步计数器（DoD #9）：删除后槽位归还。
      // 只有 ready / uploading 状态占用 countRef 额度；failed 状态早在客户端
      // 校验或服务端错误时已经 countRef -= 1，删除时不再归还（否则会减成负数语义错乱）。
      if (next.length !== prev.length && target && target.status !== "failed") {
        countRef.current = Math.max(0, countRef.current - 1);
      }
      return next;
    });
  }, []);

  /**
   * 清空所有附件并 revoke 所有 object URL。发送消息后调用。
   */
  const clear = useCallback((): void => {
    setAttachments((prev) => {
      for (const a of prev) {
        if (objectUrlsRef.current.has(a.local_preview_url)) {
          URL.revokeObjectURL(a.local_preview_url);
          objectUrlsRef.current.delete(a.local_preview_url);
        }
      }
      return [];
    });
    // 同步计数器（DoD #9）：清空后归零
    countRef.current = 0;
  }, []);

  // unmount 时兜底 revoke 所有遗留 url，防内存泄漏
  useEffect(() => {
    const objectUrls = objectUrlsRef.current;
    return () => {
      for (const url of objectUrls) {
        URL.revokeObjectURL(url);
      }
      objectUrls.clear();
    };
  }, []);

  const readyAttachments = useMemo<UserInputAttachment[]>(
    () =>
      attachments
        .filter(
          (a): a is AttachmentState & { attachment: UserInputAttachment } =>
            a.status === "ready" && a.attachment !== undefined,
        )
        .map((a) => a.attachment),
    [attachments],
  );

  const hasUploading = useMemo<boolean>(
    () => attachments.some((a) => a.status === "uploading"),
    [attachments],
  );

  const failedCount = useMemo<number>(
    () => attachments.filter((a) => a.status === "failed").length,
    [attachments],
  );

  return {
    attachments,
    upload,
    restoreReadyAttachments,
    remove,
    clear,
    readyAttachments,
    hasUploading,
    failedCount,
  };
}
