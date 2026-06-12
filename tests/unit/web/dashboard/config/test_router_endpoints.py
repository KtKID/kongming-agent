"""manage-config-tab dev-checklist #8 — 5 个 ``/api/manage/config/*`` 端点
**integration** 层 pytest（不 mock ConfigManager）。

与 ``test_router.py``（#6 smoke 层）的区别：
后者 mock 整个 ``ConfigManager`` 验证 router 的异常翻译；本文件构造**真实**
:class:`infrastructure.config.manager.ConfigManager`，搭一份 tmp yaml + 真实
``round_trip_update`` + 真 pydantic 校验 + 真 ``read_effective`` 路径，
端到端确认 HTTP ↔ 文件系统全链路：

1. ``GET /schema``     happy
2. ``GET /effective``  yaml 显式写 → ``source=yaml``
3. ``GET /effective``  yaml 没写、pydantic default → ``source=default``
4. ``GET /effective``  env override → ``source=env`` + 进入 ``env_overrides``
5. ``GET /raw``        含原文 + mtime
6. ``POST /save``      happy → 注释保留 + 文件 mtime 推进
7. ``POST /save``      mtime 冲突 → 409
8. ``POST /save``      pydantic 校验失败 → 422
9. ``POST /save``      restart_required 字段 → ``restart_required_fields`` 返回
10. ``POST /restart``  happy → 200 + restarting/pid（monkeypatch
    ``find_start_script`` + spawn 路径）
11. ``POST /restart``  脚本缺失 → 503

环境隔离策略（与 ``test_manager.py`` 一致）：

- ``monkeypatch.delenv`` 清宿主继承的 ``KONGMING_*``
- monkeypatch ``_maybe_load_env_file`` 为 no-op，防止 ``load_config`` 经
  ``dotenv.load_dotenv`` 找到仓库根 ``.env`` 重新污染 env

env override 策略：``ConfigManager.read_effective`` 每次都重读 env（内部直接
查 ``os.environ`` 且每次 ``load_config(yaml_path)`` 也重读），所以**不必重建
manager**，只需 ``monkeypatch.setenv`` 后再发 HTTP 请求即可生效。
"""

from __future__ import annotations

import time
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.dashboard.config import restart as restart_mod
from hosts.web.dashboard.config.restart import RestartScriptNotFoundError
from hosts.web.dashboard.config.router import router as dashboard_config_router
from infrastructure.config.manager import ConfigManager

router_mod = import_module("hosts.web.dashboard.config.router")

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

# 最小化 yaml：
# - 含 model 段（必填字段都给值）
# - 含 web 段（initial_password 必填 + cors_origins / llm_presets 给空 list 防
#   pydantic 抱怨）
# - 含 evolution.memory.enabled（restart_required=True 字段，用于测试 9）
# - 含 model.timeout（测试注释保留）
# - **不含** runner.max_turns，让它走 pydantic 默认值 = 10
_BASE_YAML = """\
# kongming-agent test config — integration tests
model:
  name: gpt-4o-mini
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
  temperature: 0.5
  # 这是 timeout 注释 — 保注释回归断言用
  timeout: 60
web:
  enabled: false
  host: "127.0.0.1"
  port: 60000
  dev_mode: false
  initial_password: ""
  cors_origins: []
  llm_presets: []
evolution:
  memory:
    enabled: true
"""


# 所有可能影响 sources / env_overrides 判定的 KONGMING_* env。
_ENV_VARS_TO_CLEAN: tuple[str, ...] = (
    "KONGMING_MODEL_PROVIDER",
    "KONGMING_MODEL_NAME",
    "KONGMING_MODEL_BASE_URL",
    "KONGMING_MODEL_API_KEY",
    "KONGMING_MODEL_TIMEOUT",
    "KONGMING_MODEL_MAX_TOKENS",
    "KONGMING_MODEL_TEMPERATURE",
    "KONGMING_MODEL_REASONING_EFFORT",
    "KONGMING_RUNNER_MAX_TURNS",
    "KONGMING_TRACE_RAW_LLM",
    "KONGMING_TRACE_AUTO_FLUSH",
    "KONGMING_TRACE_OUTPUT_PATH",
    "KONGMING_LOGGING_LEVEL",
    "KONGMING_WEB_ENABLED",
    "KONGMING_WEB_HOST",
    "KONGMING_WEB_PORT",
    "KONGMING_EVOLUTION_LEARNING_MODEL_NAME",
    "KONGMING_EVOLUTION_LEARNING_BASE_URL",
    "KONGMING_EVOLUTION_LEARNING_PROVIDER",
    "KONGMING_EVOLUTION_LEARNING_REASONING_EFFORT",
    "KONGMING_SCHEDULER_ENABLED",
    "KONGMING_SCHEDULER_INTERVAL",
    "KONGMING_SCHEDULER_MAX_INFLIGHT",
    "KONGMING_SCHEDULER_APPROVAL_MODE",
    "KONGMING_SCHEDULER_DEFAULT_MAX_TURNS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉所有 KONGMING_*（可能从父进程继承）+ 屏蔽 .env 自动加载。

    与 :mod:`test_manager` 共享语义；放在本文件本地 fixture 避免引入
    conftest.py（保持单测文件独立可读）。
    """
    for var in _ENV_VARS_TO_CLEAN:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "infrastructure.config.loader._maybe_load_env_file",
        lambda *_args, **_kwargs: {},
    )


@pytest.fixture
def tmp_yaml_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """写入 tmp_path/setting.yaml 并切 cwd 到 tmp_path。

    切 cwd 是为了让 ``dotenv.load_dotenv`` 找不到任何 .env 文件——双保险
    （配合 autouse ``_clean_env``）。
    """
    p = tmp_path / "setting.yaml"
    p.write_text(_BASE_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return p


@pytest.fixture
def app_with_manager(tmp_yaml_path: Path, tmp_path: Path) -> FastAPI:
    """挂 router + 真实 ConfigManager（repo_root = tmp_path，无 start.sh）。

    不走 ``web.app.create_app``——它会拉起 thread manager / auth 等沉重依赖，
    本测试只关心 dashboard.config 子模块的端到端行为，所以最小化装配。
    """
    app = FastAPI()
    app.state.config_manager = ConfigManager(yaml_path=tmp_yaml_path, env_path=tmp_path / ".env")
    app.state.config_restart_repo_root = tmp_path
    app.include_router(dashboard_config_router)
    return app


@pytest.fixture
def client(app_with_manager: FastAPI) -> TestClient:
    return TestClient(app_with_manager)


# ---------------------------------------------------------------------------
# 1) GET /schema
# ---------------------------------------------------------------------------


def test_get_schema_returns_fields_and_groups(client: TestClient) -> None:
    """GET /schema → 200，body 含非空 fields + 5 个 groups。"""
    resp = client.get("/api/manage/config/schema")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("fields"), list)
    assert len(payload["fields"]) > 0, "schema fields 应非空"
    assert isinstance(payload.get("groups"), list)
    assert len(payload["groups"]) == 5
    # group 形态：{"id": ..., "label": ...}
    for g in payload["groups"]:
        assert "id" in g
        assert "label" in g


# ---------------------------------------------------------------------------
# 2) GET /effective — yaml 显式写
# ---------------------------------------------------------------------------


def test_get_effective_yaml_source(client: TestClient) -> None:
    """yaml 显式写的 model.temperature=0.5 → values + sources='yaml'。"""
    resp = client.get("/api/manage/config/effective")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["values"]["model.temperature"] == 0.5
    assert payload["sources"]["model.temperature"] == "yaml"


# ---------------------------------------------------------------------------
# 3) GET /effective — yaml 没写、pydantic 默认值
# ---------------------------------------------------------------------------


def test_get_effective_default_source(client: TestClient) -> None:
    """yaml 没写 runner.max_turns → pydantic 默认值 + sources='default'。

    RunnerConfig.max_turns 默认 = 10（与 :mod:`test_manager` 断言一致；
    若 pydantic 模型默认改了，此处会一起捕到漂移）。
    """
    resp = client.get("/api/manage/config/effective")
    assert resp.status_code == 200
    payload = resp.json()
    assert "runner.max_turns" in payload["values"]
    assert payload["values"]["runner.max_turns"] == 10
    assert payload["sources"]["runner.max_turns"] == "default"


# ---------------------------------------------------------------------------
# 4) GET /effective — env override
# ---------------------------------------------------------------------------


def test_get_effective_env_override(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """KONGMING_RUNNER_MAX_TURNS=20 → values=20 / sources='env' / 进入 env_overrides。

    策略：ConfigManager.read_effective 每次都重新 load_config + 重读
    os.environ；monkeypatch.setenv 后**无需重建 manager**，直接发 HTTP 请求
    即生效。
    """
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "20")
    resp = client.get("/api/manage/config/effective")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["values"]["runner.max_turns"] == 20
    assert payload["sources"]["runner.max_turns"] == "env"
    assert "runner.max_turns" in payload["env_overrides"]


# ---------------------------------------------------------------------------
# 5) GET /raw
# ---------------------------------------------------------------------------


def test_get_raw_returns_content_and_mtime(client: TestClient, tmp_yaml_path: Path) -> None:
    """GET /raw → body.content 等于 tmp yaml 原文（含注释），mtime > 0。"""
    resp = client.get("/api/manage/config/raw")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["content"] == _BASE_YAML
    # 文件含注释 → content 也含注释
    assert "# 这是 timeout 注释" in payload["content"]
    assert payload["mtime"] > 0
    assert payload["path"] == str(tmp_yaml_path)


# ---------------------------------------------------------------------------
# 6) POST /save — happy path + 注释保留断言
# ---------------------------------------------------------------------------


def test_post_save_happy_preserves_comments(client: TestClient, tmp_yaml_path: Path) -> None:
    """改 model.temperature 0.5 → 0.9：
    - 返回 200 / ok=True / new_mtime > expected_mtime
    - 文件被改：含 ``0.9``
    - 关键注释行 ``# 这是 timeout 注释`` 仍然存在（ruamel round-trip 保住）
    """
    old_mtime = tmp_yaml_path.stat().st_mtime
    # macOS APFS mtime 是 ns 但 stat() 返回 float；sleep 一下保险
    time.sleep(0.01)

    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "model.temperature", "value": 0.9}],
            "expected_mtime": old_mtime,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_mtime"] > old_mtime
    # model.temperature 在 schema 内 restart_required=False → 不进列表
    assert body["restart_required_fields"] == []

    # 文件断言：值更新 + 注释保留
    new_text = tmp_yaml_path.read_text(encoding="utf-8")
    assert "0.9" in new_text
    assert "# 这是 timeout 注释" in new_text, (
        "ruamel round-trip 应保留行内注释；丢注释说明 writer 漂移"
    )
    # 顺便确认 base_url 等其他字段没有被无意改动
    assert "http://127.0.0.1:1234/v1" in new_text


# ---------------------------------------------------------------------------
# 7) POST /save — mtime 冲突 → 409
# ---------------------------------------------------------------------------


def test_post_save_mtime_conflict_returns_409(client: TestClient, tmp_yaml_path: Path) -> None:
    """先记 mtime → 外部 touch 文件 → 用旧 mtime save → 409。"""
    old_mtime = tmp_yaml_path.stat().st_mtime
    time.sleep(0.01)
    # 模拟另一个客户端（或用户手编辑器）写盘
    tmp_yaml_path.write_text(tmp_yaml_path.read_text(encoding="utf-8"), encoding="utf-8")
    assert tmp_yaml_path.stat().st_mtime > old_mtime

    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "model.temperature", "value": 0.7}],
            "expected_mtime": old_mtime,
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "mtime conflict" in detail


# ---------------------------------------------------------------------------
# 8) POST /save — pydantic 校验失败 → 422
# ---------------------------------------------------------------------------


def test_post_save_validation_failed_returns_422(client: TestClient, tmp_yaml_path: Path) -> None:
    """temperature=99 超出 [0, 2] → 422 + body.detail.errors 含字段。

    走真实 pydantic 校验（不 mock），同时断原文件被 writer 回滚 = 原样。
    """
    mtime = tmp_yaml_path.stat().st_mtime
    original_text = tmp_yaml_path.read_text(encoding="utf-8")

    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "model.temperature", "value": 99}],
            "expected_mtime": mtime,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "errors" in detail
    assert isinstance(detail["errors"], list)
    assert len(detail["errors"]) > 0
    # 至少有一条 error 提到 model.temperature
    paths = [err.get("path", "") for err in detail["errors"]]
    assert any("temperature" in p for p in paths), f"应有 temperature 相关错误，实际 paths={paths}"

    # 原文件没动（writer tmp + rename 回滚语义）
    assert tmp_yaml_path.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# 9) POST /save — restart_required 字段
# ---------------------------------------------------------------------------


def test_post_save_returns_restart_required_fields(client: TestClient, tmp_yaml_path: Path) -> None:
    """改 evolution.memory.enabled（schema 标 restart_required=True）→
    body.restart_required_fields 含该 path。

    fixture 已经在 yaml 里写好 ``evolution.memory.enabled: true``（writer 不
    补结构，path 必须先在 yaml 里存在）。
    """
    mtime = tmp_yaml_path.stat().st_mtime
    time.sleep(0.01)

    resp = client.post(
        "/api/manage/config/save",
        json={
            "patch": [{"path": "evolution.memory.enabled", "value": False}],
            "expected_mtime": mtime,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "evolution.memory.enabled" in body["restart_required_fields"]


# ---------------------------------------------------------------------------
# 10) POST /restart — happy（monkeypatch trigger_restart）
# ---------------------------------------------------------------------------


def test_post_restart_happy(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Web restart adapter 返回 fake pid → 200 + restarting/pid。

    monkeypatch 打到 ``web.dashboard.config.router.trigger_web_restart``。
    """
    fake_pid = 99887
    monkeypatch.setattr(
        router_mod,
        "trigger_web_restart",
        lambda repo_root: fake_pid,
    )
    resp = client.post("/api/manage/config/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["restarting"] is True
    assert body["pid"] == fake_pid


# ---------------------------------------------------------------------------
# 11) POST /restart — 脚本缺失 → 503
# ---------------------------------------------------------------------------


def test_post_restart_script_missing_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """让 Web restart adapter 抛 :class:`RestartScriptNotFoundError`
    → router 翻译成 503。

    只在最底层 spawn 处 monkeypatch 抛异常，验证 router 翻译 503 的链路。
    """

    def _raise_missing(repo_root: Any) -> int:
        raise RestartScriptNotFoundError(f"./start.sh missing under {repo_root}")

    monkeypatch.setattr(router_mod, "trigger_web_restart", _raise_missing)
    resp = client.post("/api/manage/config/restart")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "start.sh missing" in detail


# 防 lint 报 "imported but unused"：restart_mod 留作 module 引用，便于将来想
# 切换到 patch find_start_script 而非 trigger_restart 时不用重 import。
_ = restart_mod
