"""
CLI entry point for the agent eval suite.

Usage:
    python eval/run_eval.py                # run all tasks (real LLM calls)
    python eval/run_eval.py --tasks qa     # filter by tag substring
    python eval/run_eval.py --limit 5      # first N tasks (quick smoke)
    python eval/run_eval.py --list         # list tasks and exit
    python eval/run_eval.py --trace-base http://127.0.0.1:8080

Exit code 0 = all passed, 1 = any failed, 2 = error.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "data"))

from agent.llm_client import AgentLLMClient
from eval.report import write_reports
from eval.runner import run_eval
from eval.tasks import TASKS


def load_config() -> dict:
    import yaml
    cfg_path = PROJECT_ROOT / "data" / "config.yaml"
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}")
        sys.exit(2)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent eval suite")
    parser.add_argument("--tasks", help="only run tasks whose id/tags contain this substring (comma-separated ids ok)")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"],
                        help="only run tasks of this difficulty")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each task N times (default 1); aggregates pass rate & stability")
    parser.add_argument("--limit", type=int, help="only run the first N tasks")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--trace-base", default=os.environ.get("EVAL_TRACE_BASE_URL", "http://127.0.0.1:8080"),
                        help="base URL for trace replay links (default: http://127.0.0.1:8080)")
    parser.add_argument("--no-save", action="store_true", help="don't write report files")
    parser.add_argument("--verbose", action="store_true", help="show each task's full output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list:
        tasks = TASKS
        if args.tasks:
            wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
            tasks = [t for t in tasks if any(t.task_id == w or w in ",".join(t.tags) or w in t.task_id for w in wanted)]
        if args.difficulty:
            tasks = [t for t in tasks if t.difficulty == args.difficulty]
        for t in tasks:
            print(f"{t.task_id:20s} [{','.join(t.tags)}] {t.description}")
        return 0

    tasks = TASKS
    if args.tasks:
        # Support comma-separated task ids AND tag/substring filters
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        selected = []
        for t in TASKS:
            if any(t.task_id == w for w in wanted):
                selected.append(t)
            elif any(w in ",".join(t.tags) or w in t.task_id for w in wanted):
                selected.append(t)
        tasks = selected
        if not tasks:
            print(f"No tasks match: {args.tasks}")
            return 2
    if args.difficulty:
        tasks = [t for t in tasks if t.difficulty == args.difficulty]
        if not tasks:
            print(f"No tasks at difficulty: {args.difficulty}")
            return 2
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"Running {len(tasks)} tasks x{args.repeat} with the configured LLM backend...")

    config = load_config()
    llm = AgentLLMClient(config)

    results = run_eval(
        llm=llm, tasks=tasks, project_root=PROJECT_ROOT,
        repeat=args.repeat,
    )
    s = results["summary"]

    print()
    print("=" * 60)
    print(f"PASS RATE: {s['pass_rate']}% ({s['passed']}/{s['total']})")
    print(f"Duration: {s['total_duration']}s | Tokens: {s['total_tokens']} | Tool calls: {s['total_tool_calls']}")
    print("=" * 60)

    if args.repeat > 1 and results.get("per_task"):
        print(f"{'task':20s} {'rate':>7s} {'stable':>6s} {'avgDur':>7s}")
        for tid, agg in sorted(results["per_task"].items()):
            print(
                f"{tid:20s} {agg['pass_rate']:5.0f}% ({agg['passed']}/{agg['runs']}) "
                f"{'✅' if agg['stable'] else '⚠️':>4s} {agg['avg_duration']:6.1f}s"
            )
        print()
    for o in results["outcomes"]:
        mark = "✅" if o.passed else "❌"
        suffix = f" (run {o.run_index})" if args.repeat > 1 else ""
        print(f"{mark} {o.task_id:20s}{suffix} {o.duration:6.1f}s  {o.reason[:100]}")

    if not args.no_save:
        md_path = write_reports(results, PROJECT_ROOT / "eval" / "reports", args.trace_base)
        print(f"\nReport written: {md_path.relative_to(PROJECT_ROOT)}")

    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
