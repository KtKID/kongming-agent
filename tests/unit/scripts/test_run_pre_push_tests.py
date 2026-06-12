"""``scripts/run_pre_push_tests.py`` 单测。

覆盖 push gate 的核心契约：
1. 只选择 ``tests/unit``，过滤 e2e / integration / smoke；
2. 源码改动会映射到相关 unit 测试与稳定 smoke；
3. pytest 子进程环境会清理真实 ``KONGMING_*`` 配置；
4. 本地 nightly 脚本固定默认端口为 60999。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any


def _load_pre_push_module() -> Any:
    """通过文件路径加载脚本模块。"""

    project_root = Path(__file__).resolve().parents[3]
    script_path = project_root / "scripts" / "run_pre_push_tests.py"
    spec = importlib.util.spec_from_file_location("_run_pre_push_tests", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_run_pre_push_tests"] = module
    spec.loader.exec_module(module)
    return module


pre_push = _load_pre_push_module()


def _touch(path: Path) -> None:
    """创建测试用空文件，关键输入是目标路径。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_select_unit_tests_filters_non_unit_layers(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/config/test_config_manager.py")
    _touch(tmp_path / "tests/unit/test_config_loader.py")
    _touch(tmp_path / "tests/unit/test_arch_contracts.py")
    _touch(tmp_path / "tests/e2e/test_local_model_config.py")

    selected = pre_push.select_unit_tests(
        tmp_path,
        [
            "src/infrastructure/config/loader.py",
            "tests/e2e/test_local_model_config.py",
            "tests/integration/test_full_log_smoke.py",
            "tests/smoke/test_kongming_web_backend_exe.py",
        ],
    )

    assert "tests/unit/config/test_config_manager.py" in selected
    assert "tests/unit/test_config_loader.py" in selected
    assert "tests/unit/test_arch_contracts.py" in selected
    assert all(not path.startswith("tests/e2e/") for path in selected)
    assert all(not path.startswith("tests/integration/") for path in selected)
    assert all(not path.startswith("tests/smoke/") for path in selected)


def test_detect_base_ref_requires_existing_ref(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)

    try:
        pre_push.detect_base_ref(tmp_path, {})
    except RuntimeError as exc:
        assert "cannot find a valid diff base" in str(exc)
    else:
        raise AssertionError("detect_base_ref should fail when no base ref exists")


def test_detect_base_ref_does_not_guess_main_without_upstream(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    _touch(tmp_path / "README.md")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_path, check=True)

    try:
        pre_push.detect_base_ref(tmp_path, {})
    except RuntimeError as exc:
        assert "cannot find a valid diff base" in str(exc)
    else:
        raise AssertionError("detect_base_ref should not guess main without upstream")


def test_select_unit_tests_uses_direct_unit_test_file(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/safety/test_grant_cmd_prefix.py")

    selected = pre_push.select_unit_tests(
        tmp_path,
        ["tests/unit/safety/test_grant_cmd_prefix.py"],
    )

    assert selected == ["tests/unit/safety/test_grant_cmd_prefix.py"]


def test_select_unit_tests_keeps_web_mapping_narrow(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/test_web_routers_threads.py")
    _touch(tmp_path / "tests/unit/web/test_thread_status_ws.py")
    _touch(tmp_path / "tests/unit/web/test_webhooks_dispatcher.py")
    _touch(tmp_path / "tests/unit/web/test_unrelated.py")
    _touch(tmp_path / "tests/unit/test_config_loader.py")

    selected = pre_push.select_unit_tests(
        tmp_path,
        ["src/hosts/web/routers/threads.py"],
    )

    assert "tests/unit/test_web_routers_threads.py" in selected
    assert "tests/unit/web/test_webhooks_dispatcher.py" not in selected
    assert "tests/unit/web/test_unrelated.py" not in selected


def test_select_unit_tests_matches_nested_stem_without_glob(tmp_path: Path) -> None:
    _touch(tmp_path / "tests/unit/tools/runtime/test_registry.py")
    _touch(tmp_path / "tests/unit/test_config_loader.py")

    selected = pre_push.select_unit_tests(
        tmp_path,
        ["src/tools/runtime/registry.py"],
    )

    assert "tests/unit/tools/runtime/test_registry.py" in selected


def test_build_test_env_strips_real_kongming_settings(tmp_path: Path) -> None:
    env = pre_push.build_test_env(
        tmp_path,
        {
            "HOME": "/Users/kid",
            "PATH": "/bin",
            "PYTHONPATH": "/tmp/neighbor",
            "KONGMING_MODEL_NAME": "MiniMax-M3",
            "KONGMING_MODEL_BASE_URL": "https://example.invalid",
            "kongming_model_api_key": "lowercase-real",
            "OPENAI_API_KEY": "sk-real",
            "SOME_TOKEN": "secret",
            "AWS_ACCESS_KEY_ID": "real",
        },
    )

    assert env["PATH"] == "/bin"
    assert env["HOME"] == str(tmp_path / ".kongming" / "prepush-home")
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}:/tmp/neighbor"
    assert env["KONGMING_HOME"] == str(tmp_path / ".kongming" / "prepush-home")
    assert env["KONGMING_E2E_REAL_MODEL"] == "0"
    assert env["KONGMING_SKIP_DOTENV"] == "1"
    assert "KONGMING_MODEL_NAME" not in env
    assert "KONGMING_MODEL_BASE_URL" not in env
    assert "kongming_model_api_key" not in env
    assert "KONGMING_WEB_PORT" not in env
    assert "OPENAI_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert "AWS_ACCESS_KEY_ID" not in env


def test_changed_files_since_includes_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    _touch(tmp_path / "README.md")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL
    )

    _touch(tmp_path / "scripts" / "run_pre_push_tests.py")

    changed = pre_push.changed_files_since(tmp_path, "HEAD")

    assert "scripts/run_pre_push_tests.py" in changed


def test_changed_files_since_filters_irrelevant_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    _touch(tmp_path / "README.md")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL
    )

    _touch(tmp_path / "web" / "node_modules" / "pkg" / "index.js")
    _touch(tmp_path / "docs" / "private-note.md")
    _touch(tmp_path / "tests" / "unit" / "test_config_loader.py")

    changed = pre_push.changed_files_since(tmp_path, "HEAD")

    assert "tests/unit/test_config_loader.py" in changed
    assert "web/node_modules/pkg/index.js" not in changed
    assert "docs/private-note.md" not in changed


def test_local_nightly_defaults_to_port_60999() -> None:
    project_root = Path(__file__).resolve().parents[3]
    script = (project_root / "scripts" / "run_local_nightly.sh").read_text(encoding="utf-8")

    assert 'KONGMING_WEB_PORT="${KONGMING_WEB_PORT:-60999}"' in script
    assert ".env.e2e.local" in script
    assert ".kongming/nightly" in script
    assert "require_secure_env_file" in script
    assert "path.is_symlink()" in script
    assert "st.st_uid != os.getuid()" in script
    assert "st.st_uid != os.getuid() and st.st_uid != 0" not in script
    assert "connect_ex" in script
    assert "SO_REUSEADDR" not in script
    assert "KONGMING_[A-Z0-9_]" in script
    assert 'forbidden_value_chars = {"$", chr(96), "\\r", "\\n", "\\0"}' in script
    assert 'printf -v "$key" "%s" "$value"' in script
    assert 'cat "$lock_file"' in script


def test_pre_push_hook_runs_without_files_filter() -> None:
    project_root = Path(__file__).resolve().parents[3]
    config = (project_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook_block = config.split("- id: pre-push-unit", 1)[1]

    assert "stages: [pre-push]" in hook_block
    assert "\n        files:" not in hook_block
