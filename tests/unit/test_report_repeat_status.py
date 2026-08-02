"""repeat>1 时 report 任务明细状态渲染三档覆盖。

# 测试 render_report 在 repeat>1 场景下的状态/分数列渲染。
# 关键函数：test_stable_pass / test_partial_pass / test_stable_fail
"""

from __future__ import annotations

from evals.src.report import render_report


def _make_summary(records: list[dict]) -> dict:
    """构造最小 summary，输入 task_records 列表，输出 render_report 所需 summary dict。"""

    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    cats: dict[str, dict] = {}
    for r in records:
        cat = r["category"]
        if cat not in cats:
            cats[cat] = {"total": 0, "passed": 0, "score": 0.0}
        cats[cat]["total"] += 1
        cats[cat]["passed"] += 1 if r["passed"] else 0
        cats[cat]["score"] += r["score"]
    return {
        "run_id": "test-run",
        "suite": "test",
        "mode": "test",
        "model": "test",
        "total": total,
        "passed": passed,
        "score": passed / total if total else 0.0,
        "categories": cats,
        "repeat": 4,
    }


def _make_record(task_id: str, successes: int, n: int = 4) -> dict:
    """构造 repeat 场景 task record，输入 id 和成功次数，输出 record dict。"""

    return {
        "id": task_id,
        "category": "coding",
        "passed": successes == n,
        "score": 1.0 if successes == n else 0.0,
        "repeat": {"n": n, "successes": successes},
    }


class TestRepeatStatusRendering:
    """repeat>1 时任务明细状态渲染三档。"""

    def test_stable_pass_4_of_4(self) -> None:
        """4/4 → 稳定通过。"""

        records = [_make_record("task_a", 4)]
        md = render_report(_make_summary(records), records)
        assert "| `task_a` | 代码生成 | 稳定通过 | 4/4 |" in md

    def test_partial_pass_1_of_4(self) -> None:
        """1/4 → 部分通过 (25%)。"""

        records = [_make_record("task_b", 1)]
        md = render_report(_make_summary(records), records)
        assert "| `task_b` | 代码生成 | 部分通过 | 1/4 (25%) |" in md

    def test_stable_fail_0_of_4(self) -> None:
        """0/4 → 稳定失败。"""

        records = [_make_record("task_c", 0)]
        md = render_report(_make_summary(records), records)
        assert "| `task_c` | 代码生成 | 稳定失败 | 0/4 |" in md

    def test_partial_pass_listed_as_unstable(self) -> None:
        """1/4 应出现在不稳定样例段。"""

        records = [_make_record("task_d", 1)]
        md = render_report(_make_summary(records), records)
        assert "不稳定样例" in md
        assert "`task_d`" in md.split("不稳定样例")[1]

    def test_stable_fail_listed_as_failure(self) -> None:
        """0/4 应出现在失败样例段。"""

        records = [_make_record("task_e", 0)]
        md = render_report(_make_summary(records), records)
        assert "失败样例" in md
        assert "`task_e`" in md.split("失败样例")[1]

    def test_single_trial_unchanged(self) -> None:
        """repeat=1（无 repeat 字段）保持原有通过/失败。"""

        record = {
            "id": "task_f",
            "category": "coding",
            "passed": True,
            "score": 1.0,
        }
        summary = _make_summary([record])
        summary["repeat"] = 1
        md = render_report(summary, [record])
        assert "| `task_f` | 代码生成 | 通过 | 1.00 |" in md
