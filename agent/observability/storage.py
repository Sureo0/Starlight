"""
TraceStore - JSON-file persistence for agent traces.

One JSON file per trace under data/traces/<trace_id>.json, with an in-memory
index rebuilt from the directory on startup. Thread-safe for the small
single-user workload this app targets.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from agent.observability.trace_recorder import AgentTrace

logger = logging.getLogger("agent.observability.storage")

# Config: max traces kept on disk (oldest pruned)
MAX_TRACES = 500


class TraceStore:
    """File-backed store for agent traces."""

    def __init__(self, directory: str | Path, max_traces: int = MAX_TRACES):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_traces = max_traces
        self._lock = threading.RLock()
        # trace_id -> summary dict (no events) for cheap listing
        self._index: dict[str, dict] = {}
        self._rebuild_index()

    # ----------------------------------------------------------
    # Internal
    # ----------------------------------------------------------

    def _rebuild_index(self) -> None:
        """Rebuild the in-memory index from files on disk."""
        for path in self._dir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("trace_id"):
                    self._index[data["trace_id"]] = data
            except Exception:
                logger.warning("Skipping unreadable trace file: %s", path.name)

    def _path(self, trace_id: str) -> Path:
        return self._dir / f"{trace_id}.json"

    # ----------------------------------------------------------
    # Write
    # ----------------------------------------------------------

    def save(self, trace: AgentTrace) -> str:
        """Persist a trace; returns its trace_id."""
        trace_id = trace.trace_id
        data = trace.to_dict(with_events=True)
        with self._lock:
            self._index[trace_id] = data
            tmp = self._path(trace_id).with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                tmp.replace(self._path(trace_id))
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            self._prune_locked()
        return trace_id

    def _prune_locked(self) -> None:
        """Drop oldest traces beyond the cap (keeps the newest)."""
        if len(self._index) <= self._max_traces:
            return
        ordered = sorted(
            self._index.values(),
            key=lambda d: d.get("started_at", 0),
        )
        for old in ordered[: len(self._index) - self._max_traces]:
            self.delete(old["trace_id"])

    def delete(self, trace_id: str) -> bool:
        """Delete a trace; returns True if it existed."""
        with self._lock:
            existed = trace_id in self._index
            if existed:
                del self._index[trace_id]
            try:
                self._path(trace_id).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to delete trace file: %s", trace_id)
            return existed

    # ----------------------------------------------------------
    # Read
    # ----------------------------------------------------------

    def get(self, trace_id: str) -> AgentTrace | None:
        """Load a full trace (with events)."""
        with self._lock:
            data = self._index.get(trace_id)
            if data is None:
                return None
            events = [
                self._event_from_dict(e)
                for e in data.get("events", [])
            ]
            trace = AgentTrace(
                trace_id=data["trace_id"],
                user_message=data.get("user_message", ""),
                username=data.get("username", ""),
                conversation_id=data.get("conversation_id"),
                backend=data.get("backend", ""),
                model=data.get("model", ""),
                started_at=data.get("started_at", 0),
                finished_at=data.get("finished_at"),
                duration=data.get("duration"),
                finish_reason=data.get("finish_reason", "error"),
                finish_detail=data.get("finish_detail", ""),
                success=bool(data.get("success")),
                content=data.get("content", ""),
                tool_calls_made=int(data.get("tool_calls_made", 0)),
                iterations=int(data.get("iterations", 0)),
                plan_generated=bool(data.get("plan_generated")),
                plan_goal=data.get("plan_goal", ""),
                plan_steps=int(data.get("plan_steps", 0)),
                total_tokens=int(data.get("total_tokens", 0)),
            )
            trace.events = events
            return trace

    @staticmethod
    def _event_from_dict(e: dict) -> "TraceEvent":
        from agent.observability.trace_recorder import TraceEvent

        return TraceEvent(
            type=e.get("type", "info"),
            ts=float(e.get("ts", 0)),
            duration=e.get("duration"),
            content=e.get("content"),
            messages=e.get("messages"),
            response=e.get("response"),
            tool_calls=e.get("tool_calls"),
            mode=e.get("mode"),
            usage=e.get("usage"),
            tool=e.get("tool"),
            args=e.get("args"),
            result=e.get("result"),
            retries=e.get("retries"),
            error=e.get("error"),
            detail=e.get("detail"),
            iteration=e.get("iteration"),
        )

    def list(self, limit: int = 50, offset: int = 0, username: str | None = None,
             success: bool | None = None) -> list[dict]:
        """List trace summaries (newest first) with optional filters."""
        with self._lock:
            items = list(self._index.values())
        items.sort(key=lambda d: d.get("started_at", 0), reverse=True)
        if username:
            items = [d for d in items if d.get("username") == username]
        if success is not None:
            items = [d for d in items if bool(d.get("success")) == success]
        return [
            {k: v for k, v in d.items() if k != "events"}
            for d in items[offset:offset + limit]
        ]

    def count(self) -> int:
        with self._lock:
            return len(self._index)

