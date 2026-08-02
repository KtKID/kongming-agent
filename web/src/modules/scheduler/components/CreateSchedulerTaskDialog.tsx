/**
 * 定时任务表单弹窗 — v0.5.3 三模式
 *
 * 三种模式（v0.5.3）：
 * - create：新建（POST /api/cron/tasks）
 * - edit：编辑现有任务（PATCH /api/cron/tasks/{id}），显示 enabled toggle
 * - duplicate：基于现有任务创建副本（POST，name 加 "(副本)" 后缀），不显示 enabled
 *
 * 调度表达式四种：一次性 / 每天 / 每周 / Cron 高级
 * - 每个字段独立输入框，只允许数字
 * - 自动限制范围 + 输满位数自动跳下一框
 * - 底部实时预览计算结果
 * - 时区默认隐藏，灰字展示
 */

import { useState, useRef, useCallback, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useSchedulerStore } from "../store";
import { useThreadsStore } from "@/stores/threads";
import type {
  CreateSchedulerTaskRequest,
  SchedulerTaskVM,
  UpdateSchedulerTaskRequest,
} from "../types";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

const DAYS_OF_WEEK = ["日", "一", "二", "三", "四", "五", "六"] as const;
const DEFAULT_TIMEZONE = "Asia/Shanghai";
const DUPLICATE_SUFFIX = "（副本）";

type ScheduleMode = "once" | "daily" | "weekly" | "cron";

export type DialogMode = "create" | "edit" | "duplicate";

// ---------------------------------------------------------------------------
// 数字输入框组件
// ---------------------------------------------------------------------------

function NumField({
  value,
  onChange,
  onFocusNext,
  min,
  max,
  width = 42,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  onFocusNext?: () => void;
  min: number;
  max: number;
  width?: number;
  placeholder?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);

  const handleChange = (raw: string) => {
    // 只保留数字
    const digits = raw.replace(/\D/g, "");
    if (digits.length === 0) {
      onChange("");
      return;
    }
    const num = parseInt(digits, 10);
    // 逐位输入时允许中间态（如输入 2 准备输 25），但不超过 max
    const clamped = Math.min(num, max);
    onChange(String(clamped));
    // 输满位数自动跳
    if (digits.length >= String(max).length && onFocusNext) {
      onFocusNext();
    }
  };

  const handleBlur = () => {
    if (value === "") return;
    const num = parseInt(value, 10);
    if (num < min) onChange(String(min).padStart(String(min).length, "0"));
  };

  return (
    <input
      ref={ref}
      type="text"
      inputMode="numeric"
      className="rounded-md border border-border bg-background px-2 py-1.5 text-center font-mono text-sm tabular-nums focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
      style={{ width }}
      value={value}
      placeholder={placeholder}
      onChange={(e) => handleChange(e.target.value)}
      onBlur={handleBlur}
    />
  );
}

// ---------------------------------------------------------------------------
// 时间选择器（时:分）
// ---------------------------------------------------------------------------

function TimePicker({
  hour,
  minute,
  onHourChange,
  onMinuteChange,
  hourRef,
  minuteRef,
}: {
  hour: string;
  minute: string;
  onHourChange: (v: string) => void;
  onMinuteChange: (v: string) => void;
  hourRef?: React.RefObject<HTMLInputElement | null>;
  minuteRef?: React.RefObject<HTMLInputElement | null>;
}) {
  const internalHourRef = useRef<HTMLInputElement>(null);

  const focusMinute = useCallback(() => {
    // 延迟一帧让 React state 先更新
    requestAnimationFrame(() => {
      minuteRef?.current?.focus();
    });
  }, [minuteRef]);

  return (
    <span className="inline-flex items-center gap-0.5">
      <input
        ref={hourRef ?? internalHourRef}
        type="text"
        inputMode="numeric"
        className="rounded-md border border-border bg-background px-2 py-1.5 text-center font-mono text-sm tabular-nums focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        style={{ width: 42 }}
        placeholder="时"
        value={hour}
        onChange={(e) => {
          const digits = e.target.value.replace(/\D/g, "");
          if (digits.length === 0) { onHourChange(""); return; }
          const num = Math.min(parseInt(digits, 10), 23);
          onHourChange(String(num));
          if (digits.length >= 2) focusMinute();
        }}
        onBlur={() => {
          if (hour === "") return;
          const num = parseInt(hour, 10);
          if (num < 0) onHourChange("00");
          else onHourChange(String(num).padStart(2, "0"));
        }}
      />
      <span className="text-muted-foreground">:</span>
      <input
        ref={minuteRef}
        type="text"
        inputMode="numeric"
        className="rounded-md border border-border bg-background px-2 py-1.5 text-center font-mono text-sm tabular-nums focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        style={{ width: 42 }}
        placeholder="分"
        value={minute}
        onChange={(e) => {
          const digits = e.target.value.replace(/\D/g, "");
          if (digits.length === 0) { onMinuteChange(""); return; }
          const num = Math.min(parseInt(digits, 10), 59);
          onMinuteChange(String(num));
        }}
        onBlur={() => {
          if (minute === "") return;
          const num = parseInt(minute, 10);
          if (num < 0) onMinuteChange("00");
          else onMinuteChange(String(num).padStart(2, "0"));
        }}
      />
    </span>
  );
}

// ---------------------------------------------------------------------------
// 预览文案生成
// ---------------------------------------------------------------------------

function buildSchedulePayload(
  mode: ScheduleMode,
  fields: FormFields,
): { expr: string; scheduleType: "once" | "cron" } | null {
  const h = fields.hour.padStart(2, "0") || "00";
  const m = fields.minute.padStart(2, "0") || "00";

  switch (mode) {
    case "once": {
      const y = fields.year || String(new Date().getFullYear());
      const mo = fields.month || String(new Date().getMonth() + 1);
      const d = fields.day;
      if (!d) return null;
      const iso = `${y}-${mo.padStart(2, "0")}-${d.padStart(2, "0")}T${h}:${m}:00`;
      return { expr: iso, scheduleType: "once" };
    }
    case "daily":
      return { expr: `${m} ${h} * * *`, scheduleType: "cron" };
    case "weekly": {
      if (fields.weekday === "") return null;
      return { expr: `${m} ${h} * * ${fields.weekday}`, scheduleType: "cron" };
    }
    case "cron": {
      if (!fields.cronExpr.trim()) return null;
      return { expr: fields.cronExpr.trim(), scheduleType: "cron" };
    }
  }
  return null;
}

function buildPreviewText(
  mode: ScheduleMode,
  fields: FormFields,
): string | null {
  const h = fields.hour.padStart(2, "0") || "00";
  const m = fields.minute.padStart(2, "0") || "00";

  switch (mode) {
    case "once": {
      const y = fields.year || String(new Date().getFullYear());
      const mo = fields.month || String(new Date().getMonth() + 1);
      const d = fields.day;
      if (!d) return null;
      return `将于 ${y}-${mo.padStart(2, "0")}-${d.padStart(2, "0")} ${h}:${m} 执行`;
    }
    case "daily":
      if (!fields.hour && !fields.minute) return null;
      return `每天 ${h}:${m} 执行`;
    case "weekly": {
      if (fields.weekday === "") return null;
      return `每周${DAYS_OF_WEEK[Number(fields.weekday)]} ${h}:${m} 执行`;
    }
    case "cron": {
      const expr = fields.cronExpr.trim();
      if (!expr) return null;
      return `Cron: ${expr}`;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// 反向解析 initialTask → 表单字段
// ---------------------------------------------------------------------------
//
// 后端 triggerExpr 只有两种形态：
// - once: ISO timestamp（"2026-05-17T09:00:00" 或带时区）
// - cron: 5 字段 "m h d M w"，进一步细分 daily/weekly/cron
//
// daily = "m h * * *"
// weekly = "m h * * N"（N 是单个 0-6 数字）
// 其余都归为 cron 高级模式（让用户原样看到表达式）。

function decodeTriggerExpr(
  triggerType: SchedulerTaskVM["triggerType"],
  triggerExpr: string,
): { mode: ScheduleMode; fields: Partial<FormFields> } {
  if (triggerType === "once") {
    // ISO timestamp，可能带时区后缀；只取前 19 个字符 YYYY-MM-DDTHH:MM:SS
    const match = triggerExpr.match(
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/,
    );
    if (match) {
      return {
        mode: "once",
        fields: {
          year: match[1],
          month: String(parseInt(match[2], 10)),
          day: String(parseInt(match[3], 10)),
          hour: match[4],
          minute: match[5],
        },
      };
    }
    // 解析失败 fallback cron 模式让用户原样看
    return { mode: "cron", fields: { cronExpr: triggerExpr } };
  }

  if (triggerType === "cron") {
    const parts = triggerExpr.trim().split(/\s+/);
    if (parts.length === 5) {
      const [m, h, d, M, w] = parts;
      // daily: m h * * *
      if (d === "*" && M === "*" && w === "*") {
        return {
          mode: "daily",
          fields: { hour: h.padStart(2, "0"), minute: m.padStart(2, "0") },
        };
      }
      // weekly: m h * * N（单个 0-6 数字）
      if (d === "*" && M === "*" && /^[0-6]$/.test(w)) {
        return {
          mode: "weekly",
          fields: {
            hour: h.padStart(2, "0"),
            minute: m.padStart(2, "0"),
            weekday: w,
          },
        };
      }
    }
    return { mode: "cron", fields: { cronExpr: triggerExpr } };
  }

  // interval / seconds 等其他 triggerType（v0.5.3 不在表单里支持创建）
  // → 仍允许编辑：把表达式塞进 cron 输入框，提交时让后端 parse_schedule 处理。
  return { mode: "cron", fields: { cronExpr: triggerExpr } };
}

// ---------------------------------------------------------------------------
// 表单字段
// ---------------------------------------------------------------------------

interface FormFields {
  name: string;
  inputText: string;
  agentName: string;
  year: string;
  month: string;
  day: string;
  hour: string;
  minute: string;
  weekday: string;
  cronExpr: string;
  presetId: string;
  enabled: boolean;
}

function buildDefaultForm(): FormFields {
  const now = new Date();
  return {
    name: "",
    inputText: "",
    agentName: "default",
    year: String(now.getFullYear()),
    month: String(now.getMonth() + 1),
    day: String(now.getDate()),
    hour: String(now.getHours()).padStart(2, "0"),
    minute: String(now.getMinutes()).padStart(2, "0"),
    weekday: "",
    cronExpr: "",
    presetId: "",
    enabled: true,
  };
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface CreateSchedulerTaskDialogProps {
  open: boolean;
  mode: DialogMode;
  initialTask?: SchedulerTaskVM;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function CreateSchedulerTaskDialog({
  open,
  mode,
  initialTask,
  onClose,
}: CreateSchedulerTaskDialogProps) {
  const createTask = useSchedulerStore((s) => s.createTask);
  const updateTaskAction = useSchedulerStore((s) => s.updateTask);
  const refreshTasks = useSchedulerStore((s) => s.refreshTasks);
  const selectTask = useSchedulerStore((s) => s.selectTask);

  // preset 来源：复用全局 useThreadsStore.presets（fetch /api/presets）。
  // v0.5.4 修复：dialog 可能从 banner / cron 抽屉直接打开，不经过 ThreadList
  // mount → presets 一直是空数组。本组件 open 时主动补一次 fetchPresets。
  const presets = useThreadsStore((s) => s.presets);
  const fetchPresets = useThreadsStore((s) => s.fetchPresets);

  useEffect(() => {
    if (open && presets.length === 0) {
      void fetchPresets();
    }
  }, [open, presets.length, fetchPresets]);

  const [scheduleMode, setScheduleMode] = useState<ScheduleMode>("once");
  const [form, setForm] = useState<FormFields>(() => buildDefaultForm());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 创建成功后暂存 taskId，等 dialog 完全关闭后再刷新列表 + 选中
  const pendingTaskIdRef = useRef<string | null>(null);

  const minuteRef = useRef<HTMLInputElement>(null);
  const hourRef = useRef<HTMLInputElement>(null);

  // open 边沿触发：dialog 打开时初始化表单。
  // 用 useRef 持有上一次 open 状态避免 useEffect 把 initialTask/mode 当依赖
  // 触发重置（用户输入到一半 props 引用变化会清空字段，memory:
  // feedback_react_useeffect_unstable_callback）。
  const prevOpenRef = useRef(false);
  useEffect(() => {
    if (open && !prevOpenRef.current) {
      // 打开瞬间：根据 mode + initialTask 初始化
      const base = buildDefaultForm();
      if ((mode === "edit" || mode === "duplicate") && initialTask) {
        const decoded = decodeTriggerExpr(
          initialTask.triggerType,
          initialTask.triggerExpr,
        );
        const name =
          mode === "duplicate"
            ? `${initialTask.name}${DUPLICATE_SUFFIX}`
            : initialTask.name;
        setForm({
          ...base,
          ...decoded.fields,
          name,
          inputText: initialTask.inputText ?? "",
          agentName: initialTask.agentName || "default",
          presetId: initialTask.presetId ?? "",
          enabled: initialTask.lifecycle === "scheduled",
        });
        setScheduleMode(decoded.mode);
      } else {
        setForm(base);
        setScheduleMode("once");
      }
      setError(null);
      setSubmitting(false);
    }
    prevOpenRef.current = open;
  }, [open, mode, initialTask]);

  const update = <K extends keyof FormFields>(key: K, value: FormFields[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setError(null);
  };

  // 切换模式时不重置时间字段（用户可能从 daily 切到 cron 来微调）
  const switchMode = (m: ScheduleMode) => {
    setScheduleMode(m);
    setError(null);
  };

  const preview = buildPreviewText(scheduleMode, form);

  const handleSubmit = async () => {
    if (!form.name.trim()) { setError("请输入任务名称"); return; }
    if (!form.inputText.trim()) {
      // v0.5.4：edit 模式现可从 initialTask.inputText 预填，
      // 不再需要"留空=不修改"兜底；统一要求非空。
      setError("请输入任务内容");
      return;
    }

    const payload = buildSchedulePayload(scheduleMode, form);
    if (!payload) {
      if (scheduleMode === "once") setError("请填写执行日期和时间");
      else if (scheduleMode === "weekly") setError("请选择星期几和时间");
      else if (scheduleMode === "cron") setError("请输入 Cron 表达式");
      else setError("请填写执行时间");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      if (mode === "edit" && initialTask) {
        // PATCH 路径：只传变化的字段
        const body: UpdateSchedulerTaskRequest = {};
        if (form.name.trim() !== initialTask.name) {
          body.name = form.name.trim();
        }
        // schedule 字段：每次都传（用户可能改了模式但表达式与原 expr 巧合相同；
        // 为简化判定，统一发送让后端幂等处理）
        body.schedule = payload.expr;
        if (form.inputText.trim() !== (initialTask.inputText ?? "")) {
          body.input_text = form.inputText.trim();
        }
        if (form.presetId !== (initialTask.presetId ?? "")) {
          body.preset_id = form.presetId;
        }
        if (form.enabled !== (initialTask.lifecycle === "scheduled")) {
          body.lifecycle = form.enabled ? "scheduled" : "disabled";
        }
        const result = await updateTaskAction(initialTask.taskId, body);
        if (result) {
          setTimeout(() => onClose(), 0);
        }
      } else {
        // create / duplicate 路径：POST 新建
        const req: CreateSchedulerTaskRequest = {
          name: form.name.trim(),
          agent_name: form.agentName || "default",
          input_text: form.inputText.trim(),
          schedule_type: payload.scheduleType,
          timezone: DEFAULT_TIMEZONE,
          concurrency_policy: "forbid",
        };
        if (payload.scheduleType === "once") {
          req.once_at = payload.expr;
        } else {
          req.cron_expr = payload.expr;
        }
        if (form.presetId) {
          req.preset_id = form.presetId;
        }

        const result = await createTask(req);
        if (result) {
          // 暂存 taskId，等 useEffect 检测到 dialog 关闭后再 refresh + select
          pendingTaskIdRef.current = result.taskId;
          // 必须延迟到 macrotask：Zustand set() 同步触发
          // useSyncExternalStore forceStoreRerender，若在 passive mount
          // effects 期间被检测到 → React #185
          setTimeout(() => onClose(), 0);
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Dialog 关闭后刷新列表并选中新建任务。
  // 必须用 setTimeout 将 refreshTasks/selectTask 推迟到 passive effects
  // 阶段之后，否则 Zustand set() 在 commitHookEffectListMount 期间触发
  // useSyncExternalStore forceStoreRerender → React #185。
  useEffect(() => {
    const taskId = pendingTaskIdRef.current;
    if (!open && taskId) {
      pendingTaskIdRef.current = null;
      setTimeout(() => {
        void refreshTasks();
        selectTask(taskId);
      }, 0);
    }
  }, [open, refreshTasks, selectTask]);

  // Radix Dialog 渲染期间回调 onOpenChange，直接调 onClose() 会同步触发
  // 调用方 setState → useSyncExternalStore forceStoreRerender → React #185。
  // 用 setTimeout 将关闭推迟到 passive effects 完全结束之后。
  const handleOpenChange = useCallback((nextOpen: boolean) => {
    if (!nextOpen) {
      setTimeout(() => onClose(), 0);
    }
  }, [onClose]);

  // 模式标签
  const modes: { key: ScheduleMode; label: string }[] = [
    { key: "once", label: "一次性" },
    { key: "daily", label: "每天" },
    { key: "weekly", label: "每周" },
    { key: "cron", label: "Cron 高级" },
  ];

  // 标题 / 按钮文案
  const title =
    mode === "edit"
      ? "编辑定时任务"
      : mode === "duplicate"
        ? "复制定时任务"
        : "新建定时任务";
  const submitLabel = submitting
    ? mode === "edit"
      ? "保存中..."
      : "创建中..."
    : mode === "edit"
      ? "保存"
      : mode === "duplicate"
        ? "创建副本"
        : "创建";

  const inputTextPlaceholder = "描述任务内容，如：总结今天的日程安排";

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          {/* 任务名称 */}
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              任务名称
            </label>
            <input
              className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              placeholder="例：每天早报"
              value={form.name}
              onChange={(e) => update("name", e.target.value)}
            />
          </div>

          {/* 调度类型切换 */}
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              调度类型
            </label>
            <div className="flex gap-1.5">
              {modes.map((m) => (
                <button
                  key={m.key}
                  onClick={() => switchMode(m.key)}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                    scheduleMode === m.key
                      ? "bg-primary text-primary-foreground"
                      : "border border-border text-muted-foreground hover:bg-secondary"
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {/* --- 一次性 --- */}
          {scheduleMode === "once" && (
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                执行时间
              </label>
              <div className="flex items-center gap-1.5">
                <NumField
                  value={form.year}
                  onChange={(v) => update("year", v)}
                  min={2024}
                  max={2099}
                  width={54}
                  placeholder="年"
                />
                <span className="text-muted-foreground text-xs">-</span>
                <NumField
                  value={form.month}
                  onChange={(v) => update("month", v)}
                  min={1}
                  max={12}
                  width={36}
                  placeholder="月"
                />
                <span className="text-muted-foreground text-xs">-</span>
                <NumField
                  value={form.day}
                  onChange={(v) => update("day", v)}
                  onFocusNext={() => hourRef.current?.focus()}
                  min={1}
                  max={31}
                  width={36}
                  placeholder="日"
                />
                <span className="mx-1 text-muted-foreground">|</span>
                <TimePicker
                  hour={form.hour}
                  minute={form.minute}
                  onHourChange={(v) => update("hour", v)}
                  onMinuteChange={(v) => update("minute", v)}
                  hourRef={hourRef}
                  minuteRef={minuteRef}
                />
              </div>
            </div>
          )}

          {/* --- 每天 --- */}
          {scheduleMode === "daily" && (
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                每天执行时间
              </label>
              <div className="flex items-center gap-1">
                <span className="text-xs text-muted-foreground mr-1">每天</span>
                <TimePicker
                  hour={form.hour}
                  minute={form.minute}
                  onHourChange={(v) => update("hour", v)}
                  onMinuteChange={(v) => update("minute", v)}
                  minuteRef={minuteRef}
                />
              </div>
            </div>
          )}

          {/* --- 每周 --- */}
          {scheduleMode === "weekly" && (
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                每周执行时间
              </label>
              <div className="flex flex-col gap-2">
                <div className="flex gap-1">
                  {DAYS_OF_WEEK.map((label, idx) => (
                    <button
                      key={idx}
                      onClick={() => update("weekday", String(idx))}
                      className={`flex h-8 w-8 items-center justify-center rounded-md text-xs font-medium transition-colors ${
                        form.weekday === String(idx)
                          ? "bg-primary text-primary-foreground"
                          : "border border-border text-muted-foreground hover:bg-secondary"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-1">
                  <TimePicker
                    hour={form.hour}
                    minute={form.minute}
                    onHourChange={(v) => update("hour", v)}
                    onMinuteChange={(v) => update("minute", v)}
                    minuteRef={minuteRef}
                  />
                </div>
              </div>
            </div>
          )}

          {/* --- Cron 高级 --- */}
          {scheduleMode === "cron" && (
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                Cron 表达式
              </label>
              <input
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="分 时 日 月 周  如 0 9 * * *"
                value={form.cronExpr}
                onChange={(e) => update("cronExpr", e.target.value)}
              />
              <span className="mt-1 block text-[11px] text-muted-foreground">
                分 时 日 月 周，例：0 9 * * 1-5 = 工作日每天9点
              </span>
            </div>
          )}

          {/* 实时预览 */}
          {preview && (
            <div className="rounded-md bg-secondary/50 px-3 py-2 text-xs text-foreground">
              {preview}
            </div>
          )}

          {/* LLM Preset 下拉 */}
          <div>
            <label
              htmlFor="scheduler-preset"
              className="mb-1 block text-xs font-medium text-muted-foreground"
            >
              LLM Preset
            </label>
            <select
              id="scheduler-preset"
              aria-label="preset"
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={form.presetId}
              onChange={(e) => update("presetId", e.target.value)}
            >
              <option value="">（默认 cfg.model）</option>
              {presets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name} · {p.model}
                </option>
              ))}
            </select>
          </div>

          {/* 任务内容 */}
          <div>
            <label className="mb-1 block text-xs font-medium text-muted-foreground">
              任务内容
            </label>
            <textarea
              className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm resize-none focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              rows={3}
              placeholder={inputTextPlaceholder}
              value={form.inputText}
              onChange={(e) => update("inputText", e.target.value)}
            />
          </div>

          {/* enabled toggle —— 仅 edit 模式显示 */}
          {mode === "edit" && (
            <div className="flex items-center gap-2">
              <input
                id="scheduler-enabled"
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => update("enabled", e.target.checked)}
                aria-label="enabled"
              />
              <label
                htmlFor="scheduler-enabled"
                className="text-xs text-muted-foreground"
              >
                启用（关闭后任务不再触发，等同 disabled state）
              </label>
            </div>
          )}

          {/* 时区灰字 */}
          <div className="text-[11px] text-muted-foreground">
            时区：{DEFAULT_TIMEZONE}
          </div>

          {/* 错误信息 */}
          {error && (
            <div className="rounded-md bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button onClick={() => void handleSubmit()} disabled={submitting}>
            {submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
