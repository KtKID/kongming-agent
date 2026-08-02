"""Harness Eval 报告渲染。

# 生成中文 Markdown 评测报告、JSON 文件写入。
# 关键函数：render_report（整报告）、write_json（JSON 落盘）、
# _metrics_lines（成本与轮数段：token 总量 / 缓存命中率 / 可选成本 / 每题明细表）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CATEGORY_LABELS = {
    "coding": "代码生成",
    "instruction_following": "指令遵循",
    "long_context": "长上下文定位",
    "repo_fix": "仓库修复",
    "short_answer": "短答案推理",
    "tool_execution": "工具执行",
    "tau_tool_state": "状态化裁决",
}


def _category_label(category: str) -> str:
    """把 category 翻译为中文显示名，输入英文类别，输出中文标签。"""

    return _CATEGORY_LABELS.get(category, category)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件，输入路径和 payload，无返回值。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _failure_summary(record: dict[str, Any]) -> str:
    """根据 scorer 细节生成人类可读失败摘要，输入任务记录，输出中文说明。"""

    details = record.get("details", {})
    if isinstance(details, dict) and record.get("category") == "short_answer":
        return f"期望 `{details.get('expected')}`，实际输出 `{details.get('actual')}`。"
    if isinstance(details, dict) and record.get("category") == "tool_execution":
        failures = details.get("failures", [])
        failure_text = (
            "；".join(str(item) for item in failures) if failures else "工具执行链路不符合预期"
        )
        calls = details.get("tool_calls", [])
        results = details.get("tool_results", [])
        return (
            f"{failure_text}。实际 tool_calls: `{json.dumps(calls, ensure_ascii=False)}`；"
            f"tool_results: `{json.dumps(results, ensure_ascii=False)}`。"
        )
    if isinstance(details, dict) and record.get("category") == "tau_tool_state":
        failures = details.get("failures", [])
        failure_text = (
            "；".join(str(item) for item in failures) if failures else "最终世界状态不符合预期"
        )
        final_state = details.get("final_state", {})
        return f"{failure_text}。最终世界状态：`{json.dumps(final_state, ensure_ascii=False)}`。"
    if isinstance(details, dict) and details.get("error"):
        return str(details["error"])
    return str(record.get("error") or "未通过 scorer。")


def _analysis_lines(summary: dict[str, Any], task_records: list[dict[str, Any]]) -> list[str]:
    """生成报告分析段，输入 summary 和任务记录，输出 Markdown 行。"""

    passed_records = [record for record in task_records if record["passed"]]
    strong_categories = [
        _category_label(category)
        for category, item in sorted(summary["categories"].items())
        if item["passed"] == item["total"]
    ]
    weak_categories = [
        _category_label(category)
        for category, item in sorted(summary["categories"].items())
        if item["passed"] < item["total"]
    ]
    lines = [
        "## 能力分析",
        "",
        f"- 本轮通过 `{summary['passed']} / {summary['total']}`，总分 `{summary['score']:.2f}`。",
    ]
    if strong_categories:
        lines.append(f"- 表现稳定的能力面：{'、'.join(strong_categories)}。")
    if weak_categories:
        lines.append(f"- 暴露短板的能力面：{'、'.join(weak_categories)}。")
    if passed_records and "tool_execution" in summary["categories"]:
        lines.append(
            "- 当前样本显示该模型能完成真实 tool_call 生成、工具执行结果读取和最终答案整合。"
        )
    stable_failures = []
    unstable_records = []
    for record in task_records:
        repeat = record.get("repeat")
        if repeat and repeat["n"] > 1:
            s, n = repeat["successes"], repeat["n"]
            if s == 0:
                stable_failures.append(record)
            elif s < n:
                unstable_records.append(record)
        elif not record["passed"]:
            stable_failures.append(record)
    if stable_failures:
        lines.append("- 失败样例：")
        for record in stable_failures:
            lines.append(
                f"  - `{record['id']}`（{_category_label(record['category'])}）："
                f"{_failure_summary(record)}"
            )
    if unstable_records:
        lines.append("- 不稳定样例：")
        for record in unstable_records:
            repeat = record["repeat"]
            lines.append(
                f"  - `{record['id']}`（{_category_label(record['category'])}）："
                f"{repeat['successes']}/{repeat['n']} 通过"
            )
    return lines


def _format_cost(cost: dict[str, Any]) -> str:
    """格式化成本显示，输入 compute_cost 产出的 cost 字典，输出 `0.001234 USD` 形式字符串。"""

    return f"{float(cost['total']):.6f} {cost['currency']}"


def _metrics_lines(summary: dict[str, Any], task_records: list[dict[str, Any]]) -> list[str]:
    """渲染成本与轮数段落，输入 summary 和任务记录，输出 Markdown 行列表。

    summary 缺 metrics（旧版 run 产物）时返回空列表保持向后兼容；
    未配置 pricing 时只报 token 量，成本行明示"未配置"，不臆造单价。
    """

    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return []
    tokens = metrics.get("tokens") or {}
    prompt = int(tokens.get("prompt", 0))
    uncached = int(tokens.get("uncached_prompt", 0))
    cache_read = int(tokens.get("cache_read", 0))
    cache_write = int(tokens.get("cache_write", 0))
    completion = int(tokens.get("completion", 0))
    if prompt > 0:
        hit_rate_line = (
            f"- 缓存命中率：`{cache_read / prompt * 100:.1f}%`（cache 读 ÷ prompt 提交总量）"
        )
    else:
        hit_rate_line = "- 缓存命中率：`—`（无 prompt token 记录；fixture 伪 LLM usage 为空）"
    cost = metrics.get("cost")
    if isinstance(cost, dict):
        cost_line = f"- 估算成本：`{_format_cost(cost)}`"
    else:
        cost_line = "- 估算成本：`未配置 pricing，仅报 token 量`"
    lines = [
        "## 成本与轮数",
        "",
        f"- LLM 调用：`{int(metrics.get('llm_calls', 0))}` 次，总轮数：`{int(metrics.get('turns_total', 0))}`，"
        f"总耗时：`{int(metrics.get('duration_ms_total', 0))} ms`",
        f"- Token 总量：prompt `{prompt}`（未命中 `{uncached}` / cache 读 `{cache_read}` / "
        f"cache 写 `{cache_write}`），completion `{completion}`",
        hit_rate_line,
        cost_line,
    ]
    if int(summary.get("repeat", 1) or 1) > 1:
        lines.append(
            "- 口径：token / LLM 调用 / 时长为跨 trial 总和（本 run 总花费），下表轮数列为均值。"
        )
    has_cost = any(
        isinstance((record.get("metrics") or {}).get("cost"), dict) for record in task_records
    )
    header = "| 任务 | 轮数(均) | LLM 调用 | prompt | cache读 | cache写 | completion |"
    divider = "|---|---:|---:|---:|---:|---:|---:|"
    if has_cost:
        header += " 成本 |"
        divider += "---:|"
    lines += ["", header, divider]
    for record in task_records:
        record_metrics = record.get("metrics")
        if not isinstance(record_metrics, dict):
            continue
        record_tokens = record_metrics.get("tokens") or {}
        row = (
            f"| `{record['id']}` | {record_metrics.get('turns_mean', 0)} "
            f"| {int(record_metrics.get('llm_calls', 0))} "
            f"| {int(record_tokens.get('prompt', 0))} | {int(record_tokens.get('cache_read', 0))} "
            f"| {int(record_tokens.get('cache_write', 0))} "
            f"| {int(record_tokens.get('completion', 0))} |"
        )
        if has_cost:
            record_cost = record_metrics.get("cost")
            row += f" {_format_cost(record_cost)} |" if isinstance(record_cost, dict) else " — |"
        lines.append(row)
    lines.append("")
    return lines


def _pass_hat_k_lines(summary: dict[str, Any]) -> list[str]:
    """渲染 pass^k 可靠性段落，输入 summary，输出 Markdown 行列表。"""

    pass_hat_k = summary.get("pass_hat_k")
    note = summary.get("pass_hat_k_note")
    repeat = summary.get("repeat", 1)
    if pass_hat_k is None:
        if note:
            return ["## 可靠性（pass^k）", "", f"- {note}", ""]
        return []
    lines = [
        "## 可靠性（pass^k）",
        "",
        f"- 每题重复次数：`{repeat}`",
        "",
        "| k | pass^k |",
        "|---:|---:|",
    ]
    for k, v in sorted(pass_hat_k.items(), key=lambda x: int(x[0])):
        lines.append(f"| {k} | {v:.4f} |")
    lines.append("")
    return lines


def _trust_warning_block(summary: dict[str, Any]) -> list[str]:
    """当 preset+repeat=1 时生成不可信跑告警，输入 summary，输出 Markdown 行。"""

    if summary.get("trust_warning"):
        return [
            "## ⚠️ 警告：不可信跑",
            "",
            "本次 run 为 repeat=1 单次采样，**不能作为模型能力评估依据**。",
            "pass^k 不可计算；单次数据极易被运气干扰。",
            f"如需可信结果，重新跑：`--environment {summary.get('environment_id', '<id>')} --repeat 4` 起步。",
            "",
        ]
    return []


def render_report(summary: dict[str, Any], task_records: list[dict[str, Any]]) -> str:
    """生成中文 Markdown 报告，输入汇总和题目记录，输出 Markdown 文本。"""

    environment = summary.get("environment") if isinstance(summary.get("environment"), dict) else {}
    fixture_semantics = summary.get("fixture_semantics")
    fixture_line = ""
    if isinstance(fixture_semantics, dict):
        fixture_line = (
            "- Fixture 验证边界：`真实 Runner + 确定性伪 LLM；tool_execution 覆盖工具闭环，"
            "非工具题覆盖 runtime 请求、session 落盘和 scorer`"
        )
    category_rows = []
    for category, item in sorted(summary["categories"].items()):
        category_rows.append(
            f"| {_category_label(category)} | `{category}` | {item['passed']} / {item['total']} | {item['score']:.2f} |"
        )
    task_rows = []
    for record in task_records:
        repeat = record.get("repeat")
        if repeat and repeat["n"] > 1:
            s, n = repeat["successes"], repeat["n"]
            if s == n:
                status = "稳定通过"
                score_col = f"{s}/{n}"
            elif s == 0:
                status = "稳定失败"
                score_col = f"{s}/{n}"
            else:
                status = "部分通过"
                score_col = f"{s}/{n} ({s * 100 // n}%)"
        else:
            status = "通过" if record["passed"] else "失败"
            score_col = f"{record['score']:.2f}"
        task_rows.append(
            f"| `{record['id']}` | {_category_label(record['category'])} | {status} | {score_col} |"
        )
    return "\n".join(
        [
            "# Harness Eval 评测报告",
            "",
            *_trust_warning_block(summary),
            "## 运行信息",
            "",
            f"- 运行 ID：`{summary['run_id']}`",
            f"- 题集路径：`{summary['suite']}`",
            f"- 环境预设：`{summary.get('environment_id') or ''}`",
            f"- 运行模式：`{summary['mode']}`",
            f"- 模型 / preset：`{summary.get('model') or ''}`",
            f"- Runtime profile：`{summary.get('profile') or ''}`",
            f"- Approval mode：`{summary.get('approval_mode') or ''}`",
            f"- Session backend：`{summary.get('session_backend') or ''}`",
            f"- Compactor mode：`{summary.get('compactor_mode') or ''}`",
            f"- Runner max turns：`{summary.get('runner_max_turns') or ''}`",
            f"- Environment config path：`{environment.get('environment_config_path') or ''}`",
            f"- Environment config hash：`{environment.get('environment_config_hash') or ''}`",
            f"- Kongming config path：`{environment.get('kongming_config_path') or ''}`",
            f"- Kongming config hash：`{environment.get('kongming_config_hash') or ''}`",
            f"- Output dir：`{environment.get('output_dir') or ''}`",
            f"- API keys present：`{json.dumps(environment.get('api_keys_present') or {}, ensure_ascii=False)}`",
            f"- Override sources：`{json.dumps(environment.get('override_sources') or {}, ensure_ascii=False)}`",
            fixture_line,
            f"- 通过数：`{summary['passed']} / {summary['total']}`",
            f"- 总分：`{summary['score']:.2f}`",
            "",
            *_analysis_lines(summary, task_records),
            "",
            "## 分类得分",
            "",
            "| 能力面 | category | 通过数 | 分数 |",
            "|---|---|---:|---:|",
            *category_rows,
            "",
            "## 任务明细",
            "",
            "| 任务 | 能力面 | 状态 | 分数 |",
            "|---|---|---|---:|",
            *task_rows,
            "",
            *_metrics_lines(summary, task_records),
            *_pass_hat_k_lines(summary),
        ]
    )
