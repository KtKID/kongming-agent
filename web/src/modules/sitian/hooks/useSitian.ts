/**
 * useSitian —— 司天模块**唯一**公开 hook
 *
 * 组件只能用本 hook 访问司天状态/操作，禁止：
 *   - import "../store"
 *   - import "../api"
 *   - import "../manager"
 *
 * 这样以后改装配（如换 store 实现、走 WS 推送）只需要改 manager + hook，
 * 组件层零改动。
 */

import { useMemo } from "react";
import { sitianManager } from "../manager";
import { useSitianStore } from "../store";
import type {
  AlertPriority,
  AlertSeverity,
  ProjectStatus,
  SitianReport,
  TopAlert,
} from "../types";

// Priority / Severity 排序权重：数字越小越靠前
const PRIORITY_ORDER: Record<AlertPriority, number> = { P0: 0, P1: 1, P2: 2 };
const SEVERITY_ORDER: Record<AlertSeverity, number> = {
  high: 0,
  medium: 1,
  low: 2,
};

function compareAlerts(a: TopAlert, b: TopAlert): number {
  const pa = PRIORITY_ORDER[a.priority] ?? 99;
  const pb = PRIORITY_ORDER[b.priority] ?? 99;
  if (pa !== pb) return pa - pb;
  const sa = SEVERITY_ORDER[a.severity] ?? 99;
  const sb = SEVERITY_ORDER[b.severity] ?? 99;
  return sa - sb;
}

function dedupeProjectsByProjectId(projects: ProjectStatus[]): ProjectStatus[] {
  const seen = new Set<string>();
  const result: ProjectStatus[] = [];
  for (const project of projects) {
    if (seen.has(project.projectId)) continue;
    seen.add(project.projectId);
    result.push(project);
  }
  return result;
}

export interface UseSitianResult {
  // actions
  open: () => Promise<void>;
  close: () => void;
  refresh: () => Promise<void>;

  // state
  isOpen: boolean;
  loading: boolean;
  error: string | null;
  noReport: boolean;
  report: SitianReport | null;

  // derived
  sortedAlerts: TopAlert[];
  dedupedProjects: ProjectStatus[];
}

export function useSitian(): UseSitianResult {
  const isOpen = useSitianStore((s) => s.isOpen);
  const loading = useSitianStore((s) => s.loading);
  const error = useSitianStore((s) => s.error);
  const noReport = useSitianStore((s) => s.noReport);
  const report = useSitianStore((s) => s.report);

  const sortedAlerts = useMemo<TopAlert[]>(() => {
    if (!report) return [];
    // 防 schema 漂移：若 topAlerts 字段缺失（手工编辑/老版本写盘），spread undefined 会崩
    return [...(report.topAlerts ?? [])].sort(compareAlerts);
  }, [report]);

  const dedupedProjects = useMemo<ProjectStatus[]>(() => {
    if (!report) return [];
    return dedupeProjectsByProjectId(report.projects ?? []);
  }, [report]);

  return {
    open: sitianManager.open,
    close: sitianManager.close,
    refresh: sitianManager.refresh,
    isOpen,
    loading,
    error,
    noReport,
    report,
    sortedAlerts,
    dedupedProjects,
  };
}
