"""
Report generation for eval runs.

Produces:
  - eval/reports/<timestamp>.md   human-readable Markdown report
  - eval/reports/<timestamp>.json machine-readable data (also printed to stdout)

Supports repeat runs: per-task pass rate + stability column, difficulty
breakdown, per-run detail rows, failure details.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.runner import TaskOutcome

# Shown when the app is running; traces are served under /traces (with the
# detail view keyed by trace_id). Override via EVAL_TRACE_BASE_URL.
DEFAULT_TRACE_BASE = "http://127.0.0.1:8080"

_DIFF_EMOJI = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
_DIFF_ZH = {"easy": "简单", "medium": "中等", "hard": "困难"}


def _fmt_dur(sec: float) -> str:
    return f"{sec:.1f}s"


def build_markdown(results: dict, trace_base: str = DEFAULT_TRACE_BASE) -> str:
    s = results["summary"]
    per_task = results.get("per_task", {})
    repeat = s.get("repeat", 1)

    # Difficulty breakdown from outcome difficulty
    diff_map: dict[str, list[TaskOutcome]] = {}
    for o in results["outcomes"]:
        diff_map.setdefault(o.difficulty, []).append(o)

    lines = [
        "# Agent 评测报告",
        "",
        f"- 时间: {s['timestamp']}",
        f"- 通过率: **{s['pass_rate']}%** ({s['passed']}/{s['total']})",
        f"- 任务数: {s.get('task_count', s['total'])} | 重复次数: {repeat}",
        f"- 总耗时: {_fmt_dur(s['total_duration'])} | 总 tokens: {s['total_tokens']} | 总工具调用: {s['total_tool_calls']}",
        "",
    ]

    if diff_map:
        lines.append("## 难度分布")
        lines.append("")
        lines.append("| 难度 | 轮次 | 通过率 |")
        lines.append("| --- | --- | --- |")
        for d in ("easy", "medium", "hard"):
            runs = diff_map.get(d, [])
            if not runs:
                continue
            n_pass = sum(1 for o in runs if o.passed)
            rate = round(n_pass / len(runs) * 100, 1)
            n_tasks = len({o.task_id for o in runs})
            lines.append(f"| {_DIFF_EMOJI[d]} {_DIFF_ZH[d]} | {n_tasks} 任务 / {len(runs)} 轮次 | {rate}% ({n_pass}/{len(runs)}) |")
        lines.append("")

    if repeat > 1 and per_task:
        lines.append("## 逐任务通过率")
        lines.append("")
        lines.append("| 任务 | 结果 | 通过率 | 稳定 | 平均耗时 | 平均 Tokens | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for tid, agg in per_task.items():
            runs = [o for o in results["outcomes"] if o.task_id == tid]
            o0 = runs[0]
            trace_link = f"[回放]({trace_base}/traces?trace={agg['first_trace_id']})" if agg.get("first_trace_id") else ""
            stability = "✅" if agg["stable"] else "⚠️"
            reasons = "; ".join(sorted({r.reason[:60] for r in runs if not r.passed}))
            lines.append(
                f"| {tid} {trace_link} | {_DIFF_EMOJI.get(o0.difficulty, '')} | "
                f"{agg['pass_rate']}% ({agg['passed']}/{agg['runs']}) | {stability} | "
                f"{_fmt_dur(agg['avg_duration'])} | {agg['avg_tokens']} | {reasons[:80]} |"
            )
        lines.append("")

    lines.append("## 逐项明细")
    lines.append("")
    lines.append("| 任务 | 轮次 | 结果 | 耗时 | 工具调用 | Tokens | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for o in results["outcomes"]:
        status = "✅" if o.passed else "❌"
        trace_link = f"[回放]({trace_base}/traces?trace={o.trace_id})" if o.trace_id else ""
        reason = (o.reason or "").replace("|", "\\|")
        run_label = f"#{o.run_index}" if repeat > 1 else "-"
        lines.append(
            f"| {o.task_id} {trace_link} | {run_label} | {status} | "
            f"{_fmt_dur(o.duration)} | {o.tool_calls} | {o.tokens} | {reason[:100]} |"
        )
    lines.append("")

    # Failure details
    failed = [o for o in results["outcomes"] if not o.passed]
    if failed:
        lines.append("## 失败详情")
        lines.append("")
        for o in failed:
            lines.append(f"### {o.task_id} (run #{o.run_index})")
            lines.append("")
            lines.append(f"- 描述: {o.description}")
            lines.append(f"- 原因: {o.reason or o.error or '未知'}")
            if o.summary:
                lines.append(f"- 回复摘要: `{o.summary[:200]}`")
            if o.trace_id:
                lines.append(f"- 回放: {trace_base}/traces?trace={o.trace_id}")
            lines.append("")
    return "\n".join(lines)


def build_json(results: dict) -> str:
    return json.dumps(
        {
            "summary": results["summary"],
            "per_task": results.get("per_task", {}),
            "outcomes": [o.to_dict() for o in results["outcomes"]],
        },
        ensure_ascii=False,
        indent=2,
    )


def write_reports(results: dict, reports_dir: Path, trace_base: str = DEFAULT_TRACE_BASE) -> Path:
    """Write .md + .json reports; returns the .md path."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = results["summary"]["timestamp"].replace(":", "-").replace(" ", "_")
    md_path = reports_dir / f"eval_{ts}.md"
    json_path = reports_dir / f"eval_{ts}.json"
    md_path.write_text(build_markdown(results, trace_base), encoding="utf-8")
    json_path.write_text(build_json(results), encoding="utf-8")
    # Convenience: always point "latest" at the newest run
    for name in ("latest.md", "latest.json"):
        try:
            (reports_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    (reports_dir / "latest.md").write_text(build_markdown(results, trace_base), encoding="utf-8")
    (reports_dir / "latest.json").write_text(build_json(results), encoding="utf-8")
    return md_path
