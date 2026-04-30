"""unit：safety v0.1.4 M5.5 — :mod:`safety._grant_persister` atomic yaml 写回。

覆盖：

1. 写入 ``allow_writes`` / ``allow_tools_silent`` 列表，原值保留 + 新值追加。
2. ruamel.yaml 路径下注释 + key 顺序保留（如果 ruamel 可用）。
3. 原子写：先写 ``.tmp``，再 ``os.replace`` 重命名。
4. 父目录不存在 → ``FileNotFoundError``。
5. boundary_kind=SANDBOX 的 grant 被拒绝。
6. capability 无法路由（如 "memory"）→ ``ValueError``。
7. ruamel 不可用时 fallback pyyaml（注入 warning header，仍能写入）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from safety._grant_persister import (
    is_ruamel_available,
    persist_grant,
    persist_grants,
)
from safety.grant_store import grant_with_now
from safety.types import (
    BoundaryKind,
    DecisionSource,
    Grant,
    GrantKey,
)

# 测试用初始 yaml（含注释 + 顺序）
_INITIAL_YAML = """\
# kongming-agent test config
model:
  name: test-model
  base_url: http://127.0.0.1:11434/v1

# safety section: 用户配置追加默认规则集
safety:
  # 已存在的 allow_writes
  allow_writes:
    - "~/scratch/"

  # 已存在的工具白名单
  allow_tools_silent:
    - read_file
"""


def _write_initial_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_INITIAL_YAML, encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# 基本写入 + 列表合并
# ---------------------------------------------------------------------------


class TestPersistBasics:
    def test_appends_file_write_grant_to_allow_writes(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/kongming-extra/",
            scope="config",
            source=DecisionSource.CONFIG,
        )

        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        # 新条目入列
        assert "/tmp/kongming-extra/" in text
        # 原有条目保留
        assert "~/scratch/" in text
        # allow_tools_silent 不动
        assert "read_file" in text

    def test_appends_tool_grant_to_allow_tools_silent(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        grant = grant_with_now(
            capability="tool:list_dir",
            matcher="*",
            scope="config",
            source=DecisionSource.CONFIG,
        )

        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        assert "list_dir" in text
        # 已有的 read_file 仍在
        assert "read_file" in text

    def test_dedup_prevents_duplicate_entries(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        # 已存在的 ~/scratch/ 重复写入应去重
        grant = grant_with_now(
            capability="file_write",
            matcher="~/scratch/",
            scope="config",
            source=DecisionSource.CONFIG,
        )

        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        # ~/scratch/ 应只出现一次（在 list 里）
        assert text.count("~/scratch/") == 1

    def test_creates_safety_section_when_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  name: x\n  base_url: http://127.0.0.1\n",
            encoding="utf-8",
        )

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/x/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        assert "safety" in text
        assert "/tmp/x/" in text

    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "fresh.yaml"
        assert not config_path.exists()

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/x/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        assert config_path.exists()
        text = config_path.read_text(encoding="utf-8")
        assert "/tmp/x/" in text

    def test_batch_persist_grants(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        grants = [
            grant_with_now(
                capability="file_write",
                matcher="/tmp/a/",
                scope="config",
                source=DecisionSource.CONFIG,
            ),
            grant_with_now(
                capability="tool:list_dir",
                matcher="*",
                scope="config",
                source=DecisionSource.CONFIG,
            ),
        ]

        persist_grants(grants, config_path)

        text = config_path.read_text(encoding="utf-8")
        assert "/tmp/a/" in text
        assert "list_dir" in text


# ---------------------------------------------------------------------------
# 注释 / 顺序保留（ruamel.yaml 路径）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_ruamel_available(), reason="ruamel.yaml not installed")
class TestRoundTripComments:
    def test_preserves_top_level_comment(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/extra/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        assert "# kongming-agent test config" in text

    def test_preserves_section_inline_comment(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/extra/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        # 中文 / 英文 inline comment 都该在
        assert "用户配置追加默认规则集" in text
        assert "已存在的 allow_writes" in text

    def test_preserves_top_level_key_order(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/extra/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        text = config_path.read_text(encoding="utf-8")
        # model 仍在 safety 前面
        assert text.index("model:") < text.index("safety:")


# ---------------------------------------------------------------------------
# 原子写 + 错误路径
# ---------------------------------------------------------------------------


class TestAtomicAndErrors:
    def test_writes_via_tmp_then_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """监听 ``os.replace``：必须是 ``<config>.tmp -> <config>`` 这条路径。"""
        config_path = _write_initial_config(tmp_path)
        observed: list[tuple[str, str]] = []

        import safety._grant_persister as persister_mod

        original_replace = persister_mod.os.replace

        def fake_replace(src: str, dst: str) -> None:
            observed.append((str(src), str(dst)))
            original_replace(src, dst)

        monkeypatch.setattr(persister_mod.os, "replace", fake_replace)

        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/y/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        persist_grant(grant, config_path)

        assert len(observed) == 1
        src, dst = observed[0]
        assert src.endswith(".yaml.tmp")
        assert dst == str(config_path)

    def test_parent_dir_missing_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nested" / "more" / "config.yaml"
        # 父目录不存在
        grant = grant_with_now(
            capability="file_write",
            matcher="/tmp/z/",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        with pytest.raises(FileNotFoundError, match="parent directory"):
            persist_grant(grant, config_path)

    def test_sandbox_grant_rejected(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)

        sandbox_grant = Grant(
            key=GrantKey(
                capability="file_write",
                matcher="/tmp/sandbox/",
                boundary_kind=BoundaryKind.SANDBOX,
            ),
            scope="config",
            source=DecisionSource.CONFIG,
            created_at=0.0,
        )
        with pytest.raises(ValueError, match="HOST"):
            persist_grant(sandbox_grant, config_path)

    def test_unroutable_capability_rejected(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        grant = grant_with_now(
            capability="memory",  # 既不是 file_write 也不是 tool:*
            matcher="anywhere",
            scope="config",
            source=DecisionSource.CONFIG,
        )
        with pytest.raises(ValueError, match="cannot route capability"):
            persist_grant(grant, config_path)

    def test_empty_grants_is_noop(self, tmp_path: Path) -> None:
        config_path = _write_initial_config(tmp_path)
        original = config_path.read_text(encoding="utf-8")
        persist_grants((), config_path)
        # 文件未改动
        assert config_path.read_text(encoding="utf-8") == original
