"""unit：prompting.skills.skill_loader 装配器覆盖。

模块 A / task skill-loader-v0.1.6。覆盖 frontmatter 解析、双源扫描、
符号链接逃逸防御、listing 渲染（含 250 字截断）以及事件 fan-out。

测试矩阵对应 plan.md "测试覆盖矩阵"（24 行）。所有 IO 都用 tmp_path 动态
构造 SKILL.md，不写盘到仓库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.contracts import Event
from prompting.skills.skill_loader import (
    SkillSpec,
    format_skill_listing,
    load_skill_specs,
)

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def write_skill(skills_dir: Path, name: str, frontmatter: str, body: str = "# body") -> Path:
    """在 skills_dir/<name>/SKILL.md 写入一个 SKILL.md，返回路径。

    frontmatter 是不含 ``---`` 围栏的 YAML 文本；本辅助会自动套上 ``---\\n...\\n---``。
    """
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return target


def write_raw_skill(skills_dir: Path, name: str, raw: str) -> Path:
    """直接以 raw 文本写 SKILL.md（用于测试无 frontmatter 头等异常分支）。"""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_text(raw, encoding="utf-8")
    return target


class _RecordingSink:
    """记录收到的 Event，用于断言 emit 序列。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [ev.kind for ev in self.events]

    def of_kind(self, kind: str) -> list[Event]:
        return [ev for ev in self.events if ev.kind == kind]


class _RaisingSink:
    """每次 emit 都抛异常，验证 fan-out 不被打断。"""

    def __init__(self) -> None:
        self.called = 0

    async def emit(self, event: Event) -> None:
        self.called += 1
        raise RuntimeError("sink intentionally broken")


def _home_skills(tmp_path: Path) -> Path:
    """home 目录下 skills/ 子目录。"""
    skills = tmp_path / "home" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    return skills


def _ws_skills(tmp_path: Path) -> Path:
    """workspace 目录下 .kongming/skills/ 子目录。"""
    skills = tmp_path / "ws" / ".kongming" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    return skills


def _home_root(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _ws_root(tmp_path: Path) -> Path:
    return tmp_path / "ws"


# ---------------------------------------------------------------------------
# Frontmatter 解析（end-to-end，via load_skill_specs）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFrontmatterParsing:
    """frontmatter 解析的合法 / 异常分支，全部通过 load_skill_specs 间接覆盖。"""

    async def test_valid_minimal_frontmatter_home(self, tmp_path: Path) -> None:
        """home 单源 + 1 个 SKILL.md → 1 个 spec，source='home'。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "commit", "name: commit\ndescription: Make a commit")

        specs = await load_skill_specs(_home_root(tmp_path))

        assert len(specs) == 1
        spec = specs[0]
        assert isinstance(spec, SkillSpec)
        assert spec.name == "commit"
        assert spec.description == "Make a commit"
        assert spec.when_to_use is None
        assert spec.allowed_tools is None
        assert spec.paths is None
        assert spec.source == "home"
        assert spec.body_path.name == "SKILL.md"
        assert spec.body_path.parent.name == "commit"

    async def test_valid_frontmatter_workspace(self, tmp_path: Path) -> None:
        """workspace 单源 + 1 个 SKILL.md → 1 个 spec，source='workspace'。"""
        skills = _ws_skills(tmp_path)
        write_skill(skills, "review", "name: review\ndescription: Review PR")

        specs = await load_skill_specs(_home_root(tmp_path), _ws_root(tmp_path))

        assert len(specs) == 1
        assert specs[0].name == "review"
        assert specs[0].source == "workspace"

    async def test_missing_name_dropped_with_event(self, tmp_path: Path) -> None:
        """frontmatter 缺 name → 跳过 + emit skill.parse_failed。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "broken", "description: only desc")
        sink = _RecordingSink()

        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        assert "skill.parse_failed" in sink.kinds()
        ev = sink.of_kind("skill.parse_failed")[0]
        assert "path" in ev.payload

    async def test_missing_description_dropped_with_event(self, tmp_path: Path) -> None:
        """frontmatter 缺 description → 跳过 + emit skill.parse_failed。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "broken", "name: broken")
        sink = _RecordingSink()

        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        assert "skill.parse_failed" in sink.kinds()

    async def test_top_level_yaml_list_dropped(self, tmp_path: Path) -> None:
        """frontmatter 顶层是 yaml list（非 dict）→ 跳过 + emit parse_failed。"""
        skills = _home_skills(tmp_path)
        # frontmatter 顶层为 list
        write_skill(skills, "weird", "- item1\n- item2")
        sink = _RecordingSink()

        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        assert "skill.parse_failed" in sink.kinds()

    async def test_no_frontmatter_header_dropped(self, tmp_path: Path) -> None:
        """SKILL.md 不含 --- 围栏 → 跳过 + emit parse_failed。"""
        skills = _home_skills(tmp_path)
        write_raw_skill(skills, "plain", "# 纯 markdown\n\n没有 frontmatter\n")
        sink = _RecordingSink()

        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        assert "skill.parse_failed" in sink.kinds()

    async def test_broken_yaml_syntax_dropped(self, tmp_path: Path) -> None:
        """YAML 语法错误 → 跳过 + emit parse_failed（异常不冒泡）。"""
        skills = _home_skills(tmp_path)
        # 故意构造非法 YAML（key 后只有冒号 + 引号未闭合）
        write_skill(skills, "broken", 'name: "unterminated\ndescription: [bad: yaml')
        sink = _RecordingSink()

        # 不应抛异常
        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        assert "skill.parse_failed" in sink.kinds()

    async def test_advanced_fields_silently_ignored(self, tmp_path: Path) -> None:
        """高级字段（model / hooks / effort）出现 → 解析成功，仅 5 个字段进 SkillSpec。"""
        skills = _home_skills(tmp_path)
        write_skill(
            skills,
            "rich",
            (
                "name: rich\n"
                "description: rich skill\n"
                "model: opus\n"
                "effort: high\n"
                "hooks: foo\n"
                "context: bar\n"
                "disable-model-invocation: true\n"
                "user-invocable: false\n"
            ),
        )

        specs = await load_skill_specs(_home_root(tmp_path))

        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "rich"
        assert spec.description == "rich skill"
        # SkillSpec 只暴露 5 个 frontmatter 字段 + body_path + source；
        # 未知字段不会出现在 dataclass 上
        public_fields = {f for f in spec.__dataclass_fields__}
        assert public_fields == {
            "name",
            "description",
            "when_to_use",
            "allowed_tools",
            "paths",
            "body_path",
            "source",
        }

    async def test_field_name_dash_variant(self, tmp_path: Path) -> None:
        """字段名 dash 写法（when-to-use / allowed-tools）→ 正确解析。"""
        skills = _home_skills(tmp_path)
        write_skill(
            skills,
            "dashed",
            (
                "name: dashed\n"
                "description: with dashes\n"
                "when-to-use: When you need dashes\n"
                "allowed-tools:\n"
                "  - shell\n"
                "  - read_file\n"
                "paths:\n"
                "  - src/**/*.py\n"
            ),
        )

        specs = await load_skill_specs(_home_root(tmp_path))

        assert len(specs) == 1
        spec = specs[0]
        assert spec.when_to_use == "When you need dashes"
        assert spec.allowed_tools == ["shell", "read_file"]
        assert spec.paths == ["src/**/*.py"]

    async def test_field_name_underscore_variant(self, tmp_path: Path) -> None:
        """字段名 underscore 写法（when_to_use / allowed_tools）→ 正确解析。"""
        skills = _home_skills(tmp_path)
        write_skill(
            skills,
            "underscored",
            (
                "name: underscored\n"
                "description: with underscores\n"
                "when_to_use: When you need underscores\n"
                "allowed_tools:\n"
                "  - shell\n"
            ),
        )

        specs = await load_skill_specs(_home_root(tmp_path))

        assert len(specs) == 1
        spec = specs[0]
        assert spec.when_to_use == "When you need underscores"
        assert spec.allowed_tools == ["shell"]


# ---------------------------------------------------------------------------
# 双源加载与 shadowing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDualSourceLoading:
    """双源（home + workspace）扫描、缺目录静默、合并去重。"""

    async def test_both_empty_returns_empty_list(self, tmp_path: Path) -> None:
        """两个目录都不存在 → [] 不抛异常 / 不 emit。"""
        sink = _RecordingSink()

        specs = await load_skill_specs(
            _home_root(tmp_path),  # 不存在
            _ws_root(tmp_path),  # 不存在
            event_sinks=[sink],
        )

        assert specs == []
        assert sink.events == []

    async def test_home_missing_workspace_has_one(self, tmp_path: Path) -> None:
        """home 目录缺失，workspace 1 个 → 返回 1 个 spec。"""
        skills = _ws_skills(tmp_path)
        write_skill(skills, "ws_only", "name: ws_only\ndescription: only in ws")

        specs = await load_skill_specs(
            _home_root(tmp_path),  # 没有 home/skills/
            _ws_root(tmp_path),
        )

        assert len(specs) == 1
        assert specs[0].name == "ws_only"
        assert specs[0].source == "workspace"

    async def test_workspace_none_home_has_one(self, tmp_path: Path) -> None:
        """workspace=None，home 1 个 → 返回 1 个 spec。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "home_only", "name: home_only\ndescription: only in home")

        specs = await load_skill_specs(_home_root(tmp_path), None)

        assert len(specs) == 1
        assert specs[0].name == "home_only"
        assert specs[0].source == "home"

    async def test_dual_source_no_conflict_merges(self, tmp_path: Path) -> None:
        """两源无重名 → 返回合集。"""
        write_skill(_home_skills(tmp_path), "alpha", "name: alpha\ndescription: A")
        write_skill(_ws_skills(tmp_path), "beta", "name: beta\ndescription: B")

        specs = await load_skill_specs(_home_root(tmp_path), _ws_root(tmp_path))

        names = {s.name for s in specs}
        assert names == {"alpha", "beta"}
        sources = {s.name: s.source for s in specs}
        assert sources["alpha"] == "home"
        assert sources["beta"] == "workspace"

    async def test_dual_source_same_name_workspace_wins(self, tmp_path: Path) -> None:
        """同名 → workspace 覆盖 home + emit skill.shadowed。"""
        write_skill(_home_skills(tmp_path), "x", "name: x\ndescription: HOME version")
        write_skill(_ws_skills(tmp_path), "x", "name: x\ndescription: WS version")
        sink = _RecordingSink()

        specs = await load_skill_specs(
            _home_root(tmp_path),
            _ws_root(tmp_path),
            event_sinks=[sink],
        )

        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "x"
        assert spec.source == "workspace"
        assert spec.description == "WS version"

        shadowed = sink.of_kind("skill.shadowed")
        assert len(shadowed) == 1
        assert shadowed[0].payload["name"] == "x"
        assert shadowed[0].payload["shadowed_source"] == "home"


# ---------------------------------------------------------------------------
# 符号链接逃逸防御
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink behavior on Windows requires elevated privileges; v0.1.6 unix-only",
)
class TestSymlinkDefense:
    """body_path 必须 realpath 后落在 skill_dir 内，否则丢弃 + emit skipped。"""

    async def test_skill_md_is_symlink_to_outside_dropped(self, tmp_path: Path) -> None:
        """SKILL.md 是指向 skill_dir 外（如 /etc/passwd）的 symlink → 跳过 + emit skipped。"""
        skills = _home_skills(tmp_path)
        skill_dir = skills / "evil"
        skill_dir.mkdir()

        # 写一个真实的目标文件到 skill_dir 之外作为 symlink target
        outside = tmp_path / "outside_target.md"
        outside.write_text("---\nname: evil\ndescription: should never load\n---\n")

        link = skill_dir / "SKILL.md"
        os.symlink(outside, link)

        sink = _RecordingSink()
        specs = await load_skill_specs(_home_root(tmp_path), event_sinks=[sink])

        assert specs == []
        skipped = sink.of_kind("skill.skipped")
        assert len(skipped) == 1
        assert skipped[0].payload["reason"] == "symlink_escapes_skill_dir"


# ---------------------------------------------------------------------------
# format_skill_listing 渲染与截断
# ---------------------------------------------------------------------------


def _make_spec(
    name: str = "name",
    description: str = "desc",
    when_to_use: str | None = None,
    *,
    body_path: Path | None = None,
    source: str = "home",
) -> SkillSpec:
    """为 listing 测试构造 SkillSpec（不依赖磁盘）。"""
    return SkillSpec(
        name=name,
        description=description,
        when_to_use=when_to_use,
        allowed_tools=None,
        paths=None,
        body_path=body_path or Path("/tmp/SKILL.md"),
        source=source,  # type: ignore[arg-type]
    )


@pytest.mark.unit
class TestFormatSkillListing:
    """format_skill_listing 的所有边界：空 / 无 wtu / 有 wtu / 250 字截断 / CJK。"""

    def test_empty_specs_returns_empty_string(self) -> None:
        assert format_skill_listing([]) == ""

    def test_single_spec_without_when_to_use(self) -> None:
        spec = _make_spec(name="commit", description="Create a git commit")
        assert format_skill_listing([spec]) == "- commit: Create a git commit"

    def test_single_spec_with_when_to_use(self) -> None:
        spec = _make_spec(
            name="commit",
            description="Create a git commit",
            when_to_use="After completing a chunk",
        )
        assert (
            format_skill_listing([spec])
            == "- commit: Create a git commit - After completing a chunk"
        )

    def test_listing_is_deterministic_for_same_specs(self) -> None:
        specs = [
            _make_spec(name="commit", description="Create a git commit"),
            _make_spec(
                name="review",
                description="Review code",
                when_to_use="Before merging",
            ),
        ]

        assert format_skill_listing(specs) == format_skill_listing(specs)

    def test_boundary_exactly_250_chars_not_truncated(self) -> None:
        """desc + ' - ' + when_to_use 总长正好 250 字 → 不截断。"""
        # desc 100 + ' - ' 3 + wtu 147 = 250
        desc = "d" * 100
        wtu = "w" * 147
        spec = _make_spec(name="exact", description=desc, when_to_use=wtu)
        out = format_skill_listing([spec])
        # '- exact: ' 前缀长度 9（不参与 250 budget）
        body = out[len("- exact: ") :]
        assert len(body) == 250
        assert "…" not in body
        assert body == f"{desc} - {wtu}"

    def test_overflow_251_chars_truncated_to_249_plus_ellipsis(self) -> None:
        """251 字 → desc[:249] + '…'，整体 250 字。"""
        # desc 251 字 + 没 wtu，触发截断
        desc = "x" * 251
        spec = _make_spec(name="long", description=desc)
        out = format_skill_listing([spec])
        body = out[len("- long: ") :]
        # 截断后总长 250：249 个 'x' + 1 个 '…'
        assert len(body) == 250
        assert body.endswith("…")
        assert body[:-1] == "x" * 249

    def test_cjk_mixed_truncation_no_utf8_corruption(self) -> None:
        """中文 + emoji 混合截断 → 切片在 codepoint 边界，不破坏字符。"""
        # 构造一个超 250 codepoint 的中文 + emoji 串
        # 100 个中文 + 200 个 emoji（每个均为单 codepoint）
        chinese = "中" * 100
        emoji = "🌟" * 200
        desc = chinese + emoji
        assert len(desc) == 300  # codepoint 计数
        spec = _make_spec(name="cjk", description=desc)
        out = format_skill_listing([spec])
        body = out[len("- cjk: ") :]
        assert len(body) == 250
        assert body.endswith("…")
        # 关键断言：截断点在 codepoint 边界——可正常 utf-8 编解码、不丢字
        encoded = body.encode("utf-8")
        decoded = encoded.decode("utf-8")
        assert decoded == body

    def test_long_name_not_counted_in_budget(self) -> None:
        """name 不参与 250 字预算——250 字 desc 配长 name，name 完整保留。"""
        long_name = "very-long-skill-name-that-exceeds-typical-limits-and-keeps-going"
        # desc 正好 250 字，不触发截断
        desc = "z" * 250
        spec = _make_spec(name=long_name, description=desc)
        out = format_skill_listing([spec])
        # 整行 prefix 应是完整 name
        assert out.startswith(f"- {long_name}: ")
        body = out[len(f"- {long_name}: ") :]
        assert body == desc
        assert "…" not in body
        assert len(body) == 250


# ---------------------------------------------------------------------------
# 事件 fan-out
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventFanout:
    """event_sinks 行为：默认空、多 sink 并发、sink 抛异常隔离。"""

    async def test_default_no_sinks_runs_clean(self, tmp_path: Path) -> None:
        """event_sinks 默认 () → load 正常返回，不抛异常。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "ok", "name: ok\ndescription: ok")

        specs = await load_skill_specs(_home_root(tmp_path))

        assert len(specs) == 1
        assert specs[0].name == "ok"

    async def test_recording_sink_collects_all_four_event_kinds(self, tmp_path: Path) -> None:
        """混合场景：1 valid home + 1 broken home + home/ws 同名 conflict + 1 symlink → 4 类事件齐发。"""
        home = _home_skills(tmp_path)
        ws = _ws_skills(tmp_path)

        # 1. 有效 home spec（discovered）
        write_skill(home, "good", "name: good\ndescription: good")

        # 2. 缺 description 的 home spec（parse_failed）
        write_skill(home, "broken", "name: broken")

        # 3. shadowed：home + ws 同名（discovered for both, then shadowed）
        write_skill(home, "dup", "name: dup\ndescription: home_version")
        write_skill(ws, "dup", "name: dup\ndescription: ws_version")

        # 4. symlink 逃逸（skipped）— 仅 unix
        if sys.platform != "win32":
            evil_dir = home / "evil"
            evil_dir.mkdir()
            outside = tmp_path / "outside.md"
            outside.write_text("---\nname: evil\ndescription: outside\n---\n")
            os.symlink(outside, evil_dir / "SKILL.md")

        sink = _RecordingSink()
        await load_skill_specs(_home_root(tmp_path), _ws_root(tmp_path), event_sinks=[sink])

        kinds = sink.kinds()
        assert "skill.discovered" in kinds
        assert "skill.parse_failed" in kinds
        assert "skill.shadowed" in kinds
        if sys.platform != "win32":
            assert "skill.skipped" in kinds

        # discovered 应至少有 home/good + home/dup + ws/dup 三个
        discovered = sink.of_kind("skill.discovered")
        discovered_names = {ev.payload["name"] for ev in discovered}
        assert {"good", "dup"}.issubset(discovered_names)

    async def test_raising_sink_does_not_break_other_sinks(self, tmp_path: Path) -> None:
        """一个 sink 抛异常 → 其它 sink 仍接收，loader 仍正常返回。"""
        skills = _home_skills(tmp_path)
        write_skill(skills, "ok", "name: ok\ndescription: ok")

        broken = _RaisingSink()
        good = _RecordingSink()

        # 故意把 broken 放在 good 前面，验证它的异常不阻塞后续 fan-out
        specs = await load_skill_specs(
            _home_root(tmp_path),
            event_sinks=[broken, good],
        )

        assert len(specs) == 1
        assert specs[0].name == "ok"
        # broken 至少被调用过一次（discovered 事件）
        assert broken.called >= 1
        # good sink 应该收到了 discovered 事件
        assert "skill.discovered" in good.kinds()
