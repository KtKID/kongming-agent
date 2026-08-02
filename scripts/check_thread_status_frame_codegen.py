"""检查 ThreadStatusFrame 的 JSON Schema 与 TypeScript 生成产物是否漂移。

功能与流程：
1. 在临时目录重新导出 Pydantic serialization schema。
2. 调用仓库固定版本的 ``json2ts`` 生成 TypeScript 类型。
3. 逐字节比较临时产物与仓库提交产物，发现漂移时返回失败。

关键函数：
- ``_generate_typescript``：调用本地 json2ts 生成 TypeScript。
- ``_assert_same``：比较临时产物与仓库产物。
- ``main``：编排临时生成和双文件校验。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from export_thread_status_frame_schema import (
    DEFAULT_OUTPUT,
    DEFAULT_SNAPSHOT_OUTPUT,
    REPO_ROOT,
    export_thread_status_frame_schemas,
)

WEB_ROOT = REPO_ROOT / "web"
TYPESCRIPT_OUTPUT = WEB_ROOT / "src" / "protocol" / "generated" / "thread-status-frame.ts"
SNAPSHOT_TYPESCRIPT_OUTPUT = (
    WEB_ROOT / "src" / "protocol" / "generated" / "thread-status-snapshot-frame.ts"
)
JSON2TS = WEB_ROOT / "node_modules" / ".bin" / "json2ts"


def _generate_typescript(schema_path: Path, output_path: Path) -> None:
    """调用固定的本地 json2ts，输入为 schema 和输出路径，输出为 TypeScript 文件。"""
    if not JSON2TS.is_file():
        raise FileNotFoundError(
            f"缺少 {JSON2TS}，请先在 web 目录执行 npm ci 或 npm install",
        )
    subprocess.run(
        [
            str(JSON2TS),
            "--input",
            str(schema_path),
            "--output",
            str(output_path),
        ],
        cwd=WEB_ROOT,
        check=True,
    )


def _assert_same(actual_path: Path, expected_path: Path) -> None:
    """比较两个生成文件，输入为临时文件与仓库文件，输出为空，漂移时抛出异常。"""
    if not expected_path.is_file():
        raise FileNotFoundError(f"缺少已提交生成产物：{expected_path}")
    if actual_path.read_bytes() != expected_path.read_bytes():
        raise RuntimeError(
            f"生成产物已漂移：{expected_path}；请运行 make web-protocol-generate",
        )


def main() -> None:
    """重新生成并校验双产物，输入为空，输出为成功退出或明确漂移错误。"""
    with tempfile.TemporaryDirectory(prefix="thread-status-codegen-") as tmp:
        temporary_root = Path(tmp)
        schema_path = temporary_root / DEFAULT_OUTPUT.name
        snapshot_schema_path = temporary_root / DEFAULT_SNAPSHOT_OUTPUT.name
        typescript_path = temporary_root / TYPESCRIPT_OUTPUT.name
        snapshot_typescript_path = temporary_root / SNAPSHOT_TYPESCRIPT_OUTPUT.name
        export_thread_status_frame_schemas(
            schema_path,
            snapshot_schema_path,
        )
        _generate_typescript(schema_path, typescript_path)
        _generate_typescript(snapshot_schema_path, snapshot_typescript_path)
        _assert_same(schema_path, DEFAULT_OUTPUT)
        _assert_same(snapshot_schema_path, DEFAULT_SNAPSHOT_OUTPUT)
        _assert_same(typescript_path, TYPESCRIPT_OUTPUT)
        _assert_same(snapshot_typescript_path, SNAPSHOT_TYPESCRIPT_OUTPUT)


if __name__ == "__main__":
    main()
