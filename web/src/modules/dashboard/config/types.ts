/**
 * dashboard/config 子模块 DTO 真源。
 *
 * 与 Python `src/web/dashboard/config/{schema,manager,writer,restart}.py`
 * **严格对齐**：任一侧改字段（rename / 增/减 / 改类型 / 改枚举值）
 * 必须同步另一侧，否则 wire 层会沉默错位。
 *
 * 命名风格：interface PascalCase，字段 snake_case（直接复用后端 JSON key，
 * 不在 fetch 层做 camelCase 转换；避免双向映射出错）。
 */

export type FieldType =
  | "string"
  | "int"
  | "float"
  | "bool"
  | "enum"
  | "list"
  | "dict";

export type FieldSource = "yaml" | "env" | "default";

export interface FieldMeta {
  path: string;
  type: FieldType;
  editable: boolean;
  enum?: string[];
  desc: string;
  restart_required: boolean;
  group: string;
  min_value?: number;
  max_value?: number;
}

export interface GroupMeta {
  /** 后端 schema.groups[].id，前端按返回值动态渲染 tab。 */
  id: string;
  /** 后端 schema.groups[].label，直接作为 tab 和 section 标题。 */
  label: string;
}

export interface SchemaResponse {
  fields: FieldMeta[];
  groups: GroupMeta[];
}

export interface EffectiveResponse {
  /** 扁平：{"model.temperature": 0.7, ...}；值类型未知，统一 unknown */
  values: Record<string, unknown>;
  sources: Record<string, FieldSource>;
  env_overrides: string[];
}

export interface RawResponse {
  path: string;
  mtime: number;
  content: string;
}

export interface PatchItem {
  path: string;
  value: unknown;
}

export interface SavePatchRequest {
  patch: PatchItem[];
  expected_mtime: number;
}

export interface ValidationError {
  path: string;
  message: string;
}

export interface SavePatchResponse {
  ok: boolean;
  new_mtime: number;
  restart_required_fields: string[];
  validation_errors: ValidationError[];
}

export interface RestartResponse {
  restarting: boolean;
  pid: number;
}

export interface HealthResponse {
  status: string;
}
