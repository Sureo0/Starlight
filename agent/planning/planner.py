"""
Planner - Plan-then-Execute for the agent.

Two pieces:
  - PlanGenerator: asks the LLM to decompose a user request into an explicit
    step list (goal, ordered steps, optional notes) right before execution.
  - PlanTracker: tracks which steps are done during execution and produces a
    compact progress block that is re-injected into the context each turn so
    the model knows exactly where it is in the plan.

Design decisions:
  - Fail-soft: if planning fails (bad JSON, LLM error, trivial request), the
    agent simply runs in normal ReAct mode. Planning never blocks a chat.
  - Cheap: one extra LLM call only when the request is long enough to benefit
    (min_user_chars), max_tokens capped.
  - Explicit progress: after each tool call the orchestrator calls
    tracker.note_step() / mark_step() so the plan updates live.
"""

from __future__ import annotations

import json
import logging
import re
import threading

logger = logging.getLogger("agent.planning.planner")


# ============================================================
# Plan generation
# ============================================================

PLANNER_SYSTEM_PROMPT = """你是任务规划器。将用户的请求分解为可执行的步骤计划。

要求：
1. 步骤必须具体、可执行、顺序合理
2. 每个步骤控制在 1 句话以内
3. 步骤数不超过 8 个
4. 复杂任务(需要多步操作、信息收集、文件处理)才值得规划；简单问答直接返回空列表

只输出 JSON，不要输出任何其他文字。格式：
{"goal": "任务目标一句话", "steps": [{"description": "步骤描述", "detail": "执行提示(可选)"}]}

如果请求不需要规划，输出 {"goal": "", "steps": []}"""


class Plan:
    """A parsed execution plan."""

    def __init__(self, goal: str = "", steps: list[dict] | None = None):
        self.goal = goal
        self.steps = steps or []  # list of {"description": str, "detail": str}

    @property
    def is_empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict:
        return {"goal": self.goal, "steps": self.steps}

    def build_injection(self) -> str:
        """Build the system-prompt injection block for the plan."""
        if not self.steps:
            return ""
        lines = ["[执行计划]", f"目标：{self.goal}", "步骤："]
        for i, step in enumerate(self.steps, 1):
            desc = step.get("description", "").strip()
            detail = step.get("detail", "").strip()
            if detail:
                lines.append(f"{i}. {desc}（{detail}）")
            else:
                lines.append(f"{i}. {desc}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Plan goal={self.goal!r} steps={len(self.steps)}>"


class PlanTracker:
    """Tracks execution progress through a plan.

    Progress is normally advanced by heuristic tool-name matching in
    note_tool(). When a StepReviewer is attached (and plan_review_every
    tool calls have passed), the orchestrator calls review_progress() which
    asks the LLM to re-check the actual completed steps and can CORRECT
    heuristic mistakes (false positives AND false negatives).
    """

    def __init__(self, plan: Plan | None = None):
        self.plan = plan
        self.completed: set[int] = set()  # 0-based step indices
        self.tool_history: list[tuple[str, dict]] = []  # (tool_name, redacted_args)
        self._lock = threading.Lock()
        self.reviewer = None  # StepReviewer (optional)

    @property
    def active(self) -> bool:
        return self.plan is not None and not self.plan.is_empty

    def attach_reviewer(self, reviewer) -> None:
        self.reviewer = reviewer

    def mark_step(self, index: int) -> None:
        """Mark a step as completed (0-based)."""
        with self._lock:
            if self.plan and 0 <= index < len(self.plan.steps):
                self.completed.add(index)

    def note_tool(self, tool_name: str, tool_args: dict) -> int | None:
        """
        Heuristic: try to auto-advance the plan based on a tool call.

        If the tool call plausibly completes the current step, mark it done
        and return the new current step index (or None if unchanged).
        """
        # Record the call regardless (used by the reviewer).
        if tool_name:
            with self._lock:
                self.tool_history.append((tool_name, tool_args or {}))

        if not self.active:
            return None
        with self._lock:
            current = self._current_index()
            if current is None:
                return None
            step = self.plan.steps[current]
            desc = (step.get("description", "") + " " + step.get("detail", "")).lower()

            # Tool name appears in the step description -> done
            if tool_name and tool_name in desc:
                self.completed.add(current)
                return self._current_index()

            # Search-related steps complete on search tools
            if tool_name in ("web_search",) and ("搜索" in desc or "search" in desc or "查找" in desc or "查询" in desc):
                self.completed.add(current)
                return self._current_index()

            # File read steps complete on read_file / list_files
            if tool_name in ("read_file", "list_files") and ("读取" in desc or "查看" in desc or "读文件" in desc or "list" in desc):
                self.completed.add(current)
                return self._current_index()

            # File write steps complete on write_file
            if tool_name == "write_file" and ("写入" in desc or "写文件" in desc or "保存" in desc or "创建" in desc):
                self.completed.add(current)
                return self._current_index()

            # Code steps complete on execute_code
            if tool_name == "execute_code" and ("代码" in desc or "运行" in desc or "执行" in desc or "写代码" in desc or "脚本" in desc):
                self.completed.add(current)
                return self._current_index()

            # Weather steps complete on get_weather
            if tool_name == "get_weather" and ("天气" in desc or "气温" in desc or "温度" in desc):
                self.completed.add(current)
                return self._current_index()

            return None

    def review_progress(self) -> dict | None:
        """Ask the attached reviewer to re-check step completion.

        Returns a summary dict (or None when no reviewer / not needed):
            {"reviewed": True, "marked": [...], "unmarked": [...]}
        The reviewer is a heuristic authority: it can mark new steps done or
        UNMARK steps the heuristics wrongly marked (fail-soft: never raises).
        """
        if self.reviewer is None or not self.active:
            return None
        try:
            with self._lock:
                snapshot = {
                    "goal": self.plan.goal,
                    "steps": [
                        {
                            "index": i,
                            "description": s.get("description", ""),
                            "detail": s.get("detail", ""),
                            "completed": i in self.completed,
                        }
                        for i, s in enumerate(self.plan.steps)
                    ],
                    "completed": sorted(self.completed),
                    "tool_history": [
                        {"tool": t, "args": a} for t, a in self.tool_history[-12:]
                    ],
                }
            verdict = self.reviewer.review(snapshot)
            if verdict is None:
                return None

            changed = {"marked": [], "unmarked": []}
            with self._lock:
                for idx in verdict.get("completed", []):
                    if isinstance(idx, int) and 0 <= idx < len(self.plan.steps) and idx not in self.completed:
                        self.completed.add(idx)
                        changed["marked"].append(idx)
                for idx in verdict.get("incomplete", []):
                    if isinstance(idx, int) and idx in self.completed:
                        self.completed.discard(idx)
                        changed["unmarked"].append(idx)
            return {
                "reviewed": True,
                "goal": self.plan.goal,
                **changed,
                "detail": verdict.get("reason", ""),
            }
        except Exception as e:
            logger.warning("Plan review failed (keeping heuristic progress): %s", e)
            return None

    def _current_index(self) -> int | None:
        """Index of the first uncompleted step, or None if all done."""
        if not self.plan:
            return None
        for i in range(len(self.plan.steps)):
            if i not in self.completed:
                return i
        return None

    def current_step(self) -> dict | None:
        idx = self._current_index()
        if idx is None:
            return None
        return self.plan.steps[idx]

    def build_progress_injection(self) -> str:
        """
        Build a compact progress block for context injection.

        Example:
            [计划进度] 2/5
            [x] 步骤1
            [ ] 步骤2 <- 当前
            [ ] 步骤3
        """
        if not self.active:
            return ""
        total = len(self.plan.steps)
        lines = [f"[计划进度] 已完成 {len(self.completed)}/{total} 步"]
        current = self._current_index()
        for i, step in enumerate(self.plan.steps):
            desc = step.get("description", "").strip()
            marker = "[x]" if i in self.completed else "[ ]"
            suffix = " <- 当前" if i == current else ""
            lines.append(f"{marker} {desc}{suffix}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "goal": self.plan.goal if self.plan else "",
            "steps": [
                {
                    "description": s.get("description", ""),
                    "completed": i in self.completed,
                }
                for i, s in enumerate(self.plan.steps or [])
            ],
        }


class PlanGenerator:
    """Generates a Plan from a user request via the LLM."""

    def __init__(
        self,
        llm,
        min_user_chars: int = 30,
        max_steps: int = 8,
        timeout: int = 20,
    ):
        self._llm = llm
        self.min_user_chars = min_user_chars
        self.max_steps = max_steps
        self.timeout = timeout
        self._lock = threading.Lock()

    def should_plan(self, user_message: str) -> bool:
        """Cheap pre-filter: only plan for substantial requests."""
        if not user_message:
            return False
        # Requests with tool-relevant cues are better candidates
        cue_words = ("写", "创建", "制作", "分析", "研究", "整理", "生成", "开发", "调查",
                     "对比", "总结", "翻译", "规划", "设计", "搜索", "下载", "处理",
                     "写文件", "代码", "脚本", "报告")
        has_cue = any(w in user_message for w in cue_words)
        return len(user_message) >= self.min_user_chars or has_cue

    def generate(self, user_message: str) -> Plan:
        """Generate a plan for a request. Returns an empty Plan on any failure."""
        if not self.should_plan(user_message):
            return Plan()

        if not self._lock.acquire(blocking=False):
            return Plan()

        try:
            response = self._llm.chat(
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": f"用户请求：{user_message[:3000]}"},
                ],
                tools=None,
                tool_choice="none",
                temperature=0.2,
                max_tokens=1200,  # was 600: DeepSeek truncates mid-string on 8-step plans
                timeout=self.timeout,
            )
            return self._parse(response.content or "")
        except Exception as e:
            logger.warning("Plan generation failed (falling back to ReAct): %s", e)
            return Plan()
        finally:
            self._lock.release()

    def _parse(self, content: str) -> Plan:
        """Parse the LLM's JSON response into a Plan.

        Tolerant of truncated JSON (a common failure mode): if the raw JSON
        is unparseable because a string got cut off, try repairing the last
        unclosed string before giving up.
        """
        if not content:
            return Plan()

        text = content.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        if not text.startswith("{"):
            brace = text.find("{")
            if brace >= 0:
                text = text[brace:]

        data = self._try_load(text)
        if data is None:
            # Wrapped in prose? Extract the outermost JSON object.
            try:
                start, end = text.index("{"), text.rindex("}")
                data = self._try_load(text[start : end + 1])
            except ValueError:
                data = None
        if data is None:
            # Repair: truncation usually cuts the LAST string value mid-way.
            # Close any unclosed quotes at the end of the string.
            for repaired in self._repair_candidates(text):
                data = self._try_load(repaired)
                if data is not None:
                    logger.info("Repaired truncated planner JSON")
                    break
        if data is None:
            logger.warning("Could not parse planner output: %.120s", content)
            return Plan()

        goal = str(data.get("goal", "")).strip() if isinstance(data, dict) else ""
        raw_steps = data.get("steps", []) if isinstance(data, dict) else []
        if not isinstance(raw_steps, list):
            return Plan()

        steps = []
        for raw in raw_steps[: self.max_steps]:
            if not isinstance(raw, dict):
                continue
            desc = str(raw.get("description", "")).strip()
            if len(desc) < 3:
                continue
            detail = str(raw.get("detail", "")).strip()
            steps.append({"description": desc, "detail": detail})

        return Plan(goal=goal, steps=steps)

    @staticmethod
    def _try_load(text: str):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    @classmethod
    def _repair_candidates(cls, text: str):
        """Yield progressively-repaired versions of truncated JSON."""
        if not text:
            return
        # Case 1: cut mid-string — add a closing quote.
        if text.endswith('"'):
            yield text + '"'
        # Case 2: cut mid-string without trailing quote — close it.
        yield text + '"}'
        yield text + '"} ]}'
        yield text + '", "detail": ""}]}'


# ============================================================
# Step completion review (LLM re-check of plan progress)
# ============================================================

REVIEWER_SYSTEM_PROMPT = """你是计划进度审核员。agent 正在执行一个多步计划,以下是计划的每一步、当前标记为已完成/未完成的步骤,以及 agent 到目前为止的工具调用记录。

请根据工具调用记录,判断每一步是否真的完成了:
- 工具调用记录是完成步骤的**证据**:比如步骤要求"写入文件",而工具记录里有 write_file,该步骤就算完成。
- 如果某步被标记为已完成,但没有任何工具调用支持它(例如步骤要求"搜索",但没有任何 web_search 调用),则它应该被取消标记。
- 如果某步未被标记,但工具记录明确支持它完成,则应该补标记。
- 简单问答步骤(不需要工具)可以由 agent 的最终回复完成,但如果还没有任何执行动作,保守起见不要标记。
- 最多标记连续的前 N 步(计划是顺序执行的,跳步标记不合理)。

只输出 JSON,不要输出其他文字。格式:
{"completed": [已完成步骤的索引(0-based)], "incomplete": [应取消标记的索引], "reason": "一句话理由"}"""


class StepReviewer:
    """
    LLM-based reviewer that re-checks plan step completion.

    Called periodically during execution (see review_every in the tracker
    integration). Fail-soft: on any error it returns None and the heuristic
    progress stands.
    """

    def __init__(
        self,
        llm,
        timeout: int = 20,
        max_review_calls: int = 4,
    ):
        self._llm = llm
        self.timeout = timeout
        self.max_review_calls = max_review_calls
        self.calls = 0

    def review(self, snapshot: dict) -> dict | None:
        """Review a progress snapshot; returns a verdict dict or None."""
        if self.calls >= self.max_review_calls:
            return None
        self.calls += 1
        try:
            steps_txt = "\n".join(
                f"[{s['index']}] {'[x]' if s['completed'] else '[ ]'} {s['description']}"
                for s in snapshot["steps"]
            )
            tools_txt = "\n".join(
                f"- {t['tool']}({json.dumps(t['args'], ensure_ascii=False)[:120]})"
                for t in snapshot["tool_history"]
            ) or "(无)"

            response = self._llm.chat(
                messages=[
                    {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"计划目标: {snapshot['goal']}\n\n"
                            f"步骤状态:\n{steps_txt}\n\n"
                            f"工具调用记录:\n{tools_txt}\n\n"
                            "审核结果(JSON):"
                        ),
                    },
                ],
                tools=None,
                tool_choice="none",
                temperature=0,
                max_tokens=400,
                timeout=self.timeout,
            )
            return self._parse(response.content or "")
        except Exception as e:
            logger.warning("Step review LLM call failed: %s", e)
            return None

    def _parse(self, content: str) -> dict | None:
        """Parse the reviewer's JSON, tolerant of fences/extra text."""
        if not content:
            return None
        text = content.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                start, end = text.index("{"), text.rindex("}")
                data = json.loads(text[start : end + 1])
            except (ValueError, json.JSONDecodeError):
                logger.warning("Could not parse review output: %.120s", content)
                return None
        if not isinstance(data, dict):
            return None

        def _idx_list(key):
            vals = data.get(key)
            if not isinstance(vals, list):
                return []
            out = []
            for v in vals:
                try:
                    i = int(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < 64:  # sanity bound
                    out.append(i)
            return out

        return {
            "completed": _idx_list("completed"),
            "incomplete": _idx_list("incomplete"),
            "reason": str(data.get("reason", "")),
        }
