const WINDOWS_DRIVE_ABS_RE = /^[A-Za-z]:[\\/]/;
const WINDOWS_UNC_ABS_RE = /^\\\\[^\\]+\\[^\\]+/;

export function isAbsoluteProjectPath(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (trimmed.startsWith("/")) return true;
  if (WINDOWS_DRIVE_ABS_RE.test(trimmed)) return true;
  return WINDOWS_UNC_ABS_RE.test(trimmed);
}
