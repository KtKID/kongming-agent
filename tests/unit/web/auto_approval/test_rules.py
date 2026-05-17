"""rules.py 单测：dataclass + yaml 加载 + 校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.auto_approval.rules import (
    SUPPORTED_MATCH_KINDS,
    RuleDefinition,
    RuleSet,
    load_default_rules,
    materialize_user_rules_yaml,
)

# ---------- RuleDefinition ----------


class TestRuleDefinition:
    def test_valid_construction(self) -> None:
        r = RuleDefinition(
            id="foo",
            description="test",
            match={"kind": "bash_cmd_first", "command": "rm"},
        )
        assert r.id == "foo"
        assert r.enabled is True  # 默认

    def test_empty_id_raises(self) -> None:
        with pytest.raises(ValueError, match="id must be non-empty"):
            RuleDefinition(id="", description="x", match={"kind": "bash_cmd_first"})

    def test_unsupported_match_kind_raises(self) -> None:
        with pytest.raises(ValueError, match=r"unsupported match\.kind"):
            RuleDefinition(
                id="bad",
                description="x",
                match={"kind": "not_a_real_kind"},
            )

    def test_match_missing_kind_raises(self) -> None:
        with pytest.raises(ValueError, match=r"unsupported match\.kind"):
            RuleDefinition(id="bad", description="x", match={"command": "rm"})

    def test_frozen_immutable(self) -> None:
        r = RuleDefinition(id="x", description="d", match={"kind": "bash_cmd_first"})
        with pytest.raises(Exception):
            r.id = "y"  # type: ignore[misc]


# ---------- load_default_rules：内置 yaml ----------


class TestLoadDefaultRulesBuiltin:
    def test_loads_default_yaml(self) -> None:
        rs = load_default_rules()
        assert isinstance(rs, RuleSet)
        assert rs.version == 1
        assert rs.default_timeout_ms == 10000

    def test_default_has_24_rules(self) -> None:
        # v1.0.1: 加 bash_rm_any + bash_eval_source = 24 条
        rs = load_default_rules()
        assert len(rs.rules) == 24, (
            f"expected 24 rules, got {len(rs.rules)}: {[r.id for r in rs.rules]}"
        )

    def test_all_default_rules_enabled(self) -> None:
        rs = load_default_rules()
        for r in rs.rules:
            assert r.enabled is True, f"rule {r.id} should be enabled by default"

    def test_all_rule_ids_unique(self) -> None:
        rs = load_default_rules()
        ids = [r.id for r in rs.rules]
        assert len(ids) == len(set(ids))

    def test_all_match_kinds_supported(self) -> None:
        rs = load_default_rules()
        for r in rs.rules:
            assert r.match["kind"] in SUPPORTED_MATCH_KINDS

    def test_critical_rule_ids_present(self) -> None:
        """核心高频危险规则必须在 default 集中。"""
        rs = load_default_rules()
        ids = {r.id for r in rs.rules}
        required = {
            "bash_rm_any",  # v1.0.1 新增：任何 rm 都拦
            "bash_rm_recursive",
            "bash_sudo",
            "bash_git_push_force",
            "bash_git_reset_hard",
            "bash_chmod_recursive",
            "bash_chown_recursive",
            "bash_eval_source",  # v1.0.1 新增：eval/source/.
            "edit_dotenv",
            "edit_ssh_dir",
            "edit_etc",
            "mcp_unknown",
        }
        assert required.issubset(ids), f"missing critical rules: {required - ids}"

    def test_find_returns_rule(self) -> None:
        rs = load_default_rules()
        r = rs.find("bash_sudo")
        assert r is not None
        assert r.id == "bash_sudo"

    def test_find_missing_returns_none(self) -> None:
        rs = load_default_rules()
        assert rs.find("nonexistent") is None


# ---------- load_default_rules：错误路径 ----------


class TestLoadRulesErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_default_rules(tmp_path / "nope.yaml")

    def test_root_not_mapping_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a mapping"):
            load_default_rules(p)

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            "default_timeout_ms: 1000\nrules: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'version' must be int"):
            load_default_rules(p)

    def test_negative_timeout_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            "version: 1\ndefault_timeout_ms: -1\nrules: []\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'default_timeout_ms'"):
            load_default_rules(p)

    def test_duplicate_id_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            "version: 1\ndefault_timeout_ms: 1000\n"
            "rules:\n"
            "  - id: foo\n    description: x\n    match: {kind: bash_cmd_first, command: rm}\n"
            "  - id: foo\n    description: y\n    match: {kind: bash_cmd_first, command: dd}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate rule id"):
            load_default_rules(p)

    def test_rule_missing_id_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            "version: 1\ndefault_timeout_ms: 1000\n"
            "rules:\n  - description: x\n    match: {kind: bash_cmd_first}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing 'id'"):
            load_default_rules(p)


# ---------- materialize_user_rules_yaml ----------


class TestMaterializeUserRulesYaml:
    """v1.0.1：物化内置 yaml 到用户可编辑位置。"""

    def test_first_run_copies_yaml(self, tmp_path: Path) -> None:
        target = materialize_user_rules_yaml(tmp_path)
        assert target.exists()
        assert target == tmp_path / "web" / "auto_approval" / "rules.yaml"
        # 内容应与内置一致
        text = target.read_text(encoding="utf-8")
        assert "bash_rm_any" in text
        assert "version: 1" in text

    def test_idempotent_does_not_overwrite_user_edits(self, tmp_path: Path) -> None:
        """已存在时不应覆盖（保留用户改动）。"""
        # 首次复制
        target = materialize_user_rules_yaml(tmp_path)
        # 模拟用户改动
        marker = "# USER CUSTOM MARKER\n"
        target.write_text(marker, encoding="utf-8")
        # 再次调用不应覆盖
        materialize_user_rules_yaml(tmp_path)
        assert target.read_text(encoding="utf-8") == marker

    def test_materialized_loadable(self, tmp_path: Path) -> None:
        """物化后的 yaml 必须能用 load_default_rules 加载。"""
        target = materialize_user_rules_yaml(tmp_path)
        rs = load_default_rules(target)
        assert len(rs.rules) == 24

    def test_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        """不存在中间目录时应创建。"""
        deep_home = tmp_path / "deep" / "nested" / "home"
        target = materialize_user_rules_yaml(deep_home)
        assert target.exists()
        assert target.parent.parent == deep_home / "web"
