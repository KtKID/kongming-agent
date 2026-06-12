"""本地 pre-push 快速测试入口。

本脚本只负责 push 前的快速单元测试门禁：
1. 根据当前分支和上游分支计算本次待 push 的改动文件；
2. 从改动中选择稳定的 ``tests/unit`` 测试，过滤 e2e / integration / smoke；
3. 清理真实 ``KONGMING_*`` 环境变量，使用独立 ``.kongming/prepush-home``；
4. 调用 pytest，并打印 base、改动文件、测试集合和慢测试信息。

关键函数：
- ``detect_base_ref``：选择当前分支用于 diff 的基准引用。
- ``changed_files_since``：读取基准引用到 HEAD 的改动文件。
- ``select_unit_tests``：把改动文件映射成可重复的 unit 测试集合。
- ``build_test_env``：构造不依赖本机真实配置的 pytest 环境。
- ``run_pytest``：执行最终 pytest 命令并返回退出码。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

EXCLUDED_TEST_PREFIXES = (
    "tests/e2e/",
    "tests/integration/",
    "tests/smoke/",
)

UNIT_TEST_PREFIX = "tests/unit/"

ROOT_CONFIG_FILES = {
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "Makefile",
    "README.md",
}

SENSITIVE_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "NPM_TOKEN",
    "OPENAI_API_KEY",
    "SSH_AUTH_SOCK",
}

SENSITIVE_ENV_PREFIXES = (
    "AWS_",
    "AZURE_",
    "GCP_",
    "GIT_",
    "GOOGLE_",
)

SMOKE_TESTS = (
    "tests/unit/test_config_loader.py",
    "tests/unit/test_arch_contracts.py",
    "tests/unit/test_runtime_home_static_guards.py",
)

MODULE_TEST_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "src/infrastructure/config/",
        ("tests/unit/config/", "tests/unit/config_loader/", "tests/unit/test_config_loader.py"),
    ),
    ("src/hosts/web/", ()),
    ("src/safety/", ("tests/unit/safety/", "tests/unit/test_safety_")),
    (
        "src/tools/",
        (
            "tests/unit/tools/",
            "tests/unit/test_tool",
            "tests/unit/test_build_default_registry_skill.py",
        ),
    ),
    (
        "src/scheduler/",
        (
            "tests/unit/scheduler/",
            "tests/unit/test_web_cron",
            "tests/unit/test_cli_main_scheduler.py",
        ),
    ),
    ("src/application/", ("tests/unit/test_agent_workflow", "tests/unit/test_workflow")),
    ("src/memory/", ("tests/unit/tools/builtin/test_memory_tool.py", "tests/unit/test_memory")),
    ("src/network/", ("tests/unit/network/",)),
    (
        "src/sessions/",
        (
            "tests/unit/test_session",
            "tests/unit/test_sqlite_session.py",
            "tests/unit/test_file_session.py",
        ),
    ),
    (
        "src/core/",
        ("tests/unit/test_core", "tests/unit/test_runner", "tests/unit/test_input_assembler.py"),
    ),
    (
        "src/prompting/",
        ("tests/unit/test_prompt", "tests/unit/test_instruction", "tests/unit/prompting/"),
    ),
)


def repo_root() -> Path:
    """返回脚本所在仓库根目录。"""

    return Path(__file__).resolve().parents[1]


def _run_git(repo: Path, args: Sequence[str], *, check: bool = True) -> str:
    """执行 git 命令并返回 stdout 文本。"""

    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def _ref_exists(repo: Path, ref: str) -> bool:
    """判断 git ref 是否存在。"""

    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def _current_branch(repo: Path) -> str:
    """读取当前分支名，detached HEAD 时返回空字符串。"""

    return _run_git(repo, ["branch", "--show-current"], check=False)


def _upstream_ref(repo: Path) -> str | None:
    """读取当前分支的 upstream ref。"""

    upstream = _run_git(
        repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False
    )
    return upstream or None


def detect_base_ref(repo: Path, environ: Mapping[str, str] | None = None) -> str:
    """选择 pre-push diff 基准，优先使用显式环境变量和当前分支 upstream。"""

    env = environ or os.environ
    explicit = env.get("KONGMING_PRE_PUSH_BASE") or env.get("PRE_PUSH_BASE")
    if explicit and _ref_exists(repo, explicit):
        return explicit

    upstream = _upstream_ref(repo)
    if upstream and _ref_exists(repo, upstream):
        return upstream

    branch = _current_branch(repo)
    candidates = []
    if branch == "private-main":
        candidates.append("origin/private-main")
    candidates.extend(("origin/private-main", "origin/main", "main"))
    for ref in candidates:
        if _ref_exists(repo, ref):
            return ref
    raise RuntimeError(
        "pre-push: cannot find a valid diff base; set KONGMING_PRE_PUSH_BASE "
        "to an existing ref such as origin/main"
    )


def changed_files_since(repo: Path, base_ref: str) -> list[str]:
    """读取 base 到 HEAD 以及工作区中的新增、修改、重命名文件列表。"""

    outputs = [
        _run_git(
            repo, ["diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD"], check=False
        ),
        _run_git(repo, ["diff", "--cached", "--name-only", "--diff-filter=ACMR"], check=False),
        _run_git(repo, ["diff", "--name-only", "--diff-filter=ACMR"], check=False),
        _run_git(repo, ["ls-files", "--others", "--exclude-standard"], check=False),
    ]
    files = {
        line.strip()
        for output in outputs
        for line in output.splitlines()
        if line.strip() and _is_gate_relevant_path(line.strip())
    }
    return sorted(files)


def _is_gate_relevant_path(path: str) -> bool:
    """判断路径是否值得进入 push gate 选择和日志输出。"""

    if path in ROOT_CONFIG_FILES:
        return True
    return path.startswith(("src/", "tests/unit/", "scripts/"))


def _is_python_unit_test(path: str) -> bool:
    """判断路径是否为 unit 测试 Python 文件。"""

    return path.startswith(UNIT_TEST_PREFIX) and path.endswith(".py")


def _is_excluded_test(path: str) -> bool:
    """判断路径是否属于 pre-push 过滤的测试层。"""

    return path.startswith(EXCLUDED_TEST_PREFIXES)


def _all_unit_test_paths(repo: Path) -> list[str]:
    """返回当前仓库所有 unit 测试文件。"""

    unit_root = repo / "tests" / "unit"
    if not unit_root.is_dir():
        return []
    return sorted(
        str(path.relative_to(repo)) for path in unit_root.rglob("test_*.py") if path.is_file()
    )


def _tests_under(repo: Path, relative_dir: str) -> set[str]:
    """返回某个 unit 测试目录下的测试文件。"""

    test_dir = repo / relative_dir
    if not test_dir.is_dir():
        return set()
    return {str(path.relative_to(repo)) for path in test_dir.rglob("test_*.py") if path.is_file()}


def _tests_with_prefix(repo: Path, prefix: str) -> set[str]:
    """返回 unit 测试中路径以指定前缀开头的测试文件。"""

    return {path for path in _all_unit_test_paths(repo) if path.startswith(prefix)}


def _tests_matching_stem(repo: Path, stem: str, *, web: bool = False) -> set[str]:
    """按文件 stem 递归匹配 unit 测试，避免 ``**`` glob 语义漂移。"""

    selected: set[str] = set()
    normalized = stem.replace("_", "-")
    for test_path in _all_unit_test_paths(repo):
        test_name = Path(test_path).name
        test_stem = Path(test_path).stem
        comparable = test_stem.replace("_", "-")
        if test_name == f"test_{stem}.py" or comparable.startswith(f"test-{normalized}"):
            selected.add(test_path)
            continue
        if web and (comparable.endswith(f"-{normalized}") or f"-{normalized}-" in comparable):
            selected.add(test_path)
    return selected


def _select_from_hint(repo: Path, hint: str) -> set[str]:
    """把一个显式 hint 展开成测试文件。"""

    hint_path = repo / hint
    if hint_path.is_file() and _is_python_unit_test(hint):
        return {hint}
    if hint_path.is_dir():
        return _tests_under(repo, hint)
    return _tests_with_prefix(repo, hint)


def _source_related_tests(repo: Path, source_path: str) -> set[str]:
    """根据源码或脚本路径生成候选 unit 测试。"""

    stem = Path(source_path).stem
    selected: set[str] = set()
    if source_path.startswith("scripts/"):
        script_test = f"tests/unit/scripts/test_{stem}.py"
        if (repo / script_test).is_file():
            selected.add(script_test)
    if source_path.startswith("src/"):
        selected.update(
            _tests_matching_stem(repo, stem, web=source_path.startswith("src/hosts/web/"))
        )
        for prefix, mapped_hints in MODULE_TEST_HINTS:
            if source_path.startswith(prefix):
                for hint in mapped_hints:
                    selected.update(_select_from_hint(repo, hint))
    return selected


def _existing_smoke_tests(repo: Path) -> set[str]:
    """返回仓库中存在的快速 smoke unit 测试集合。"""

    return {path for path in SMOKE_TESTS if (repo / path).is_file()}


def select_unit_tests(repo: Path, changed_files: Sequence[str]) -> list[str]:
    """把改动文件映射成 push gate 应运行的 unit 测试路径。"""

    selected: set[str] = set()
    source_changed = False
    config_changed = False

    for path in changed_files:
        if _is_excluded_test(path):
            continue
        if _is_python_unit_test(path):
            selected.add(path)
            continue
        if path in ROOT_CONFIG_FILES:
            config_changed = True
            continue
        if path.startswith(("src/", "scripts/")) and path.endswith((".py", ".sh")):
            source_changed = True
            selected.update(_source_related_tests(repo, path))

    if source_changed or config_changed:
        selected.update(_existing_smoke_tests(repo))
    if not selected:
        selected.update(_existing_smoke_tests(repo))

    return sorted(selected)


def build_test_env(repo: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造隔离 pytest 环境，清除真实 ``KONGMING_*`` 和常见凭证配置。"""

    source = environ or os.environ
    env = {key: value for key, value in source.items() if not _is_sensitive_env_name(key)}
    home = repo / ".kongming" / "prepush-home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["KONGMING_HOME"] = str(home)
    env["KONGMING_E2E_REAL_MODEL"] = "0"
    env["KONGMING_SKIP_DOTENV"] = "1"
    env["PYTHONPATH"] = _prepend_pythonpath(repo / "src", env.get("PYTHONPATH"))
    return env


def _is_sensitive_env_name(name: str) -> bool:
    """判断环境变量名是否属于 push gate 需要隔离的真实配置或凭证。"""

    if name.startswith("KONGMING_"):
        return True
    if name in SENSITIVE_ENV_NAMES:
        return True
    if name.endswith("_TOKEN") or name.endswith("_API_KEY"):
        return True
    return name.startswith(SENSITIVE_ENV_PREFIXES)


def _prepend_pythonpath(src_path: Path, current: str | None) -> str:
    """把仓库 src 放到 PYTHONPATH 最前，同时保留调用方已有搜索路径。"""

    src_text = str(src_path)
    if not current:
        return src_text
    parts = current.split(os.pathsep)
    if src_text in parts:
        return current
    return os.pathsep.join([src_text, current])


def run_pytest(repo: Path, tests: Sequence[str]) -> int:
    """执行隔离后的 pytest 命令。"""

    if not tests:
        print("pre-push: no unit tests selected")
        return 0

    command = [
        "uv",
        "run",
        "pytest",
        *tests,
        "--import-mode=importlib",
        "--maxfail=5",
        "--tb=short",
        "--durations=20",
        "-q",
        "-W",
        "ignore::pytest.PytestUnraisableExceptionWarning",
    ]
    print("pre-push pytest command:")
    print("  " + shlex.join(command))
    try:
        return subprocess.run(
            command,
            cwd=repo,
            env=build_test_env(repo),
            check=False,
            timeout=600,
        ).returncode
    except subprocess.TimeoutExpired:
        print("pre-push pytest timed out after 600 seconds", file=sys.stderr)
        return 124


def main() -> int:
    """命令行入口，完成 diff、选择测试并执行 pytest。"""

    repo = repo_root()
    try:
        base_ref = detect_base_ref(repo)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    changed = changed_files_since(repo, base_ref)
    selected = select_unit_tests(repo, changed)

    print(f"pre-push base: {base_ref}")
    _print_path_list("changed files", changed)
    _print_path_list("selected unit tests", selected)
    sys.stdout.flush()

    return run_pytest(repo, selected)


def _print_path_list(title: str, paths: Sequence[str], *, limit: int = 50) -> None:
    """打印有限数量的路径，长分支输出保持可读。"""

    print(f"{title}: {len(paths)}")
    for path in paths[:limit]:
        print(f"  {path}")
    remaining = len(paths) - limit
    if remaining > 0:
        print(f"  ... and {remaining} more")


if __name__ == "__main__":
    raise SystemExit(main())
