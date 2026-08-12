"""
AI Chat - A lightweight AI chat application
Built with Flask, supports multiple LLM backends
"""
import json
import os
import sys
import uuid
import time
import base64
import mimetypes
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import yaml
import re
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory, session, Response

# Add data/ to path for modules (database, backup, alerts)
sys.path.insert(0, str(Path(__file__).parent / "data"))

from auth import auth_bp, login_required
from backup import run_backup, restore_backup, list_backups
from alerts import AlertEngine
from database import db

# Agent module
from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.presets import create_agent
from agent.llm_client import AgentLLMClient
from agent.memory.service import MemoryService
from agent.observability.storage import TraceStore
from agent.approval import ApprovalManager, ApprovalStore
from agent.scheduler import AgentScheduler, TaskStore, RUN_SUCCESS
from agent.cancellation import manager as cancellation_manager, DIRECT, CONFIRM


# ============================================================
# CSRF Protection
# ============================================================
def generate_csrf_token():
    """Generate and store a CSRF token in the session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = os.urandom(32).hex()
    return session["_csrf_token"]


def validate_csrf_token():
    """Validate the CSRF token from the request against the session."""
    token = request.headers.get("X-CSRF-Token") or (
        request.json.get("_csrf_token") if request.is_json else None
    )
    expected = session.get("_csrf_token")
    if not expected or not token or not hmac.compare_digest(token, expected):
        return False
    return True


# ============================================================
# Logging
# ============================================================
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Main logger
logger = logging.getLogger("ai-chat")
logger.setLevel(logging.INFO)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_console)

# Rotating file handler - daily rotation, keep 30 days
_file_handler = TimedRotatingFileHandler(
    LOG_DIR / "app.log", when="midnight", backupCount=30, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)

# Access log - separate file for request/response tracking.
# propagate=False keeps per-request INFO lines OUT of the console
# (they still go to logs/access.log); errors surface via error_logger
# and the ai-chat WARNING+ handlers.
access_logger = logging.getLogger("ai-chat.access")
access_logger.setLevel(logging.INFO)
access_logger.propagate = False
_access_handler = TimedRotatingFileHandler(
    LOG_DIR / "access.log", when="midnight", backupCount=30, encoding="utf-8"
)
_access_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
access_logger.addHandler(_access_handler)

# Error log - separate file for errors only
error_logger = logging.getLogger("ai-chat.error")
error_logger.setLevel(logging.WARNING)
_error_handler = TimedRotatingFileHandler(
    LOG_DIR / "error.log", when="midnight", backupCount=30, encoding="utf-8"
)
_error_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
error_logger.addHandler(_error_handler)


# ============================================================
# Metrics collector
# ============================================================
class Metrics:
    """In-memory metrics collector for monitoring."""

    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = datetime.now(timezone.utc)
        self.total_requests = 0
        self.error_count = 0
        self.status_codes = {}          # {200: 123, 404: 5, ...}
        self.llm_calls = 0
        self.llm_errors = 0
        self.llm_total_tokens = 0
        self.llm_total_duration = 0.0   # seconds
        self.llm_calls_by_model = {}    # {"deepseek-chat": 10, ...}
        self.chat_messages = 0
        self.conversations_created = 0
        self.login_attempts = 0
        self.login_failures = 0
        self.active_users = set()
        self.request_paths = {}         # {"/api/chat": 50, ...}

    def record_request(self, path, status_code, duration):
        with self._lock:
            self.total_requests += 1
            self.status_codes[status_code] = self.status_codes.get(status_code, 0) + 1
            if status_code >= 400:
                self.error_count += 1
            # Track path usage (strip query params and IDs)
            route = self._normalize_path(path)
            self.request_paths[route] = self.request_paths.get(route, 0) + 1

    def record_llm_call(self, model, duration, tokens_used):
        with self._lock:
            self.llm_calls += 1
            self.llm_total_duration += duration
            self.llm_total_tokens += tokens_used
            self.llm_calls_by_model[model] = self.llm_calls_by_model.get(model, 0) + 1

    def record_llm_error(self):
        with self._lock:
            self.llm_errors += 1

    def record_chat_message(self):
        with self._lock:
            self.chat_messages += 1

    def record_conversation_created(self):
        with self._lock:
            self.conversations_created += 1

    def record_login(self, username, success):
        with self._lock:
            self.login_attempts += 1
            if not success:
                self.login_failures += 1
            if success and username:
                self.active_users.add(username)

    def _normalize_path(self, path):
        """Normalize path: remove IDs and query strings."""
        parts = path.split("?")[0].rstrip("/").split("/")
        normalized = []
        for p in parts:
            if len(normalized) >= 3 and len(p) == 12 and all(c in "0123456789abcdef" for c in p):
                normalized.append("{id}")
            else:
                normalized.append(p)
        return "/".join(normalized) or "/"

    def get_stats(self):
        with self._lock:
            uptime = datetime.now(timezone.utc) - self.start_time
            uptime_seconds = int(uptime.total_seconds())
            avg_llm_duration = (
                self.llm_total_duration / self.llm_calls if self.llm_calls > 0 else 0
            )
            return {
                "uptime_seconds": uptime_seconds,
                "uptime_human": self._format_uptime(uptime_seconds),
                "start_time": self.start_time.isoformat(),
                "total_requests": self.total_requests,
                "error_count": self.error_count,
                "status_codes": dict(self.status_codes),
                "llm_calls": self.llm_calls,
                "llm_errors": self.llm_errors,
                "llm_total_tokens": self.llm_total_tokens,
                "llm_avg_duration_ms": round(avg_llm_duration * 1000, 1),
                "llm_calls_by_model": dict(self.llm_calls_by_model),
                "chat_messages": self.chat_messages,
                "conversations_created": self.conversations_created,
                "login_attempts": self.login_attempts,
                "login_failures": self.login_failures,
                "active_users": len(self.active_users),
                "top_paths": dict(
                    sorted(self.request_paths.items(), key=lambda x: -x[1])[:10]
                ),
            }

    def _format_uptime(self, seconds):
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)


metrics = Metrics()


# ============================================================
# Configuration
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.yaml"
CONVERSATIONS_DIR = DATA_DIR / "conversations"
BACKUP_DIR = DATA_DIR / "backups"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB per file

DATA_DIR.mkdir(parents=True,exist_ok=True)
CONVERSATIONS_DIR.mkdir(parents=True,exist_ok=True)

# Alert engine (initialized after metrics)
alert_engine = AlertEngine(app_metrics=metrics)

# Thread-safe config lock
_config_lock = threading.RLock()

# Env-var backed API keys: env name -> resolved value (populated on load so
# save_config can write back "${ENV}" placeholders instead of the real key).
_env_api_keys: dict = {}


def _load_dotenv():
    """Load KEY=VALUE pairs from .env (if present) into os.environ.

    Keys are never written to config.yaml — they live in the environment
    (or the git-ignored .env file) and are referenced from config.yaml as
    ${ENV_VAR_NAME} placeholders.
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                os.environ.setdefault(k, v)
    except Exception as e:
        logger.warning("Failed to load .env: %s", e)


def _resolve_env_api_keys(cfg: dict) -> dict:
    """Resolve `${ENV_VAR}` api_key placeholders to real keys in memory."""
    backends = cfg.get("llms", {}).get("backends", [])
    for b in backends:
        key = (b.get("api_key") or "").strip()
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", key)
        if m:
            env_name = m.group(1)
            b["api_key_env"] = env_name
            b["api_key"] = os.environ.get(env_name, "")
            _env_api_keys[env_name] = b["api_key"]
            if not b["api_key"]:
                logger.warning(
                    "Environment variable %s not set for backend '%s'",
                    env_name, b.get("name"),
                )
    return cfg


def _restore_env_api_keys(cfg: dict) -> dict:
    """Rewrite resolved keys back to `${ENV_VAR}` placeholders before saving.

    A backend key that still equals its env-var value is written as the
    placeholder (never the real key). A NEW key the user typed explicitly
    is stored as-is.
    """
    backends = cfg.get("llms", {}).get("backends", [])
    for b in backends:
        env_name = b.pop("api_key_env", None)
        key = b.get("api_key") or ""
        if env_name and key == _env_api_keys.get(env_name):
            b["api_key"] = f"${{{env_name}}}"
    return cfg


def load_config():
    with _config_lock:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return _resolve_env_api_keys(yaml.safe_load(f) or {})
    return {}


def save_config(cfg):
    with _config_lock:
        # Deep-copy so placeholder-restoration never mutates the caller's
        # in-memory config (which must keep the RESOLVED keys for LLM calls).
        cfg = _restore_env_api_keys(json.loads(json.dumps(cfg)))
        tmp = CONFIG_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            # Windows: 杀毒软件/Defender 实时防护会短暂锁定新生成的 tmp 文件,
            # 导致 os.replace 抛 PermissionError (WinError 5)。小延迟重试几次。
            last_exc = None
            for _attempt in range(5):
                try:
                    tmp.replace(CONFIG_FILE)
                    last_exc = None
                    break
                except PermissionError as e:
                    last_exc = e
                    time.sleep(0.2 * (_attempt + 1))
            if last_exc is not None:
                raise last_exc
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise


def _env_name_for_backend(name: str) -> str:
    """Derive an env var name from a backend name, e.g. 'DeepSeek' -> 'DEEPSEEK_API_KEY'."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_").upper()
    base = base or "LLM"
    return f"{base}_API_KEY"


def _write_env_value(name: str, value: str) -> bool:
    """Upsert KEY=VALUE in the .env file and os.environ (fail-soft)."""
    try:
        env_file = BASE_DIR / ".env"
        lines = []
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{name}="):
                lines[i] = f"{name}={value}"
                found = True
                break
        if not found:
            lines.append(f"{name}={value}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[name] = value
        _env_api_keys[name] = value
        return True
    except Exception as e:
        logger.warning("Failed to write %s to .env: %s", name, e)
        return False


# ============================================================
# Conversation Storage (SQLite)
# ============================================================

def _validate_conv_id(conv_id):
    """Validate conversation ID format."""
    if not conv_id or not all(c in "0123456789abcdef" for c in conv_id):
        return False
    if len(conv_id) < 8 or len(conv_id) > 16:
        return False
    return True


def get_conversations():
    """List all conversations"""
    return db.list_conversations()


def load_conversation(conv_id):
    """Load a conversation by ID."""
    if not _validate_conv_id(conv_id):
        return None
    return db.get_conversation(conv_id)


def save_conversation(conv_id, data):
    """Save conversation data to database."""
    if not _validate_conv_id(conv_id):
        raise ValueError(f"Invalid conversation ID: {conv_id}")

    title = data.get("title", "New Chat")
    messages = data.get("messages", [])

    # Create or update conversation
    existing = db.get_conversation(conv_id)
    if not existing:
        db.create_conversation(conv_id, title=title)
    else:
        db.update_conversation(conv_id, title=title)

    # Replace all messages (simple approach - delete and re-insert)
    conn = db._get_conn()
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("timestamp", datetime.now(timezone.utc).isoformat())
        if role in ("user", "assistant", "system") and content:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, timestamp, reasoning) VALUES (?, ?, ?, ?, ?)",
                (conv_id, role, content, ts, msg.get("reasoning")),
            )
    conn.commit()


def delete_conversation(conv_id):
    """Delete a conversation."""
    if not _validate_conv_id(conv_id):
        return
    db.delete_conversation(conv_id)


# ============================================================
# LLM Backend (with connection pooling)
# ============================================================
class LLMClient:
    """Unified LLM client supporting OpenAI-compatible APIs"""

    def __init__(self, config):
        with _config_lock:
            self.config = config
        # Reusable connection pool
        self._session = requests.Session()
        self._adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=0,
        )
        self._session.mount("https://", self._adapter)
        self._session.mount("http://", self._adapter)

    def chat(self, messages, model=None, temperature=0.7, max_tokens=2048):
        """Send chat request to LLM"""
        backend = self._get_active_backend()
        if not backend:
            raise ValueError("No LLM backend configured. Please add one in Settings.")

        api_base = backend.get("api_base", "https://api.openai.com/v1").rstrip("/")
        api_key = backend.get("api_key", "")
        model = model or backend.get("model", "gpt-3.5-turbo")

        if not api_key:
            raise ValueError(f"API key not set for backend '{backend.get('name', '')}'")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._session.post(
            f"{api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"LLM API error ({resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "content": content,
            "model": data.get("model", model),
            "usage": usage,
        }

    def list_models(self):
        """List available models from the active backend"""
        backend = self._get_active_backend()
        if not backend:
            return []

        api_base = backend.get("api_base", "").rstrip("/")
        api_key = backend.get("api_key", "")

        try:
            resp = self._session.get(
                f"{api_base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
        return []

    def _get_active_backend(self):
        """Get the currently active backend.

        Priority:
          1. The backend named by config['active_backend'] (if it exists)
          2. The first enabled backend
          3. The first backend
        """
        backends = self.config.get("llms", {}).get("backends", [])
        if not backends:
            return None
        active_name = self.config.get("active_backend")
        if active_name:
            for b in backends:
                if b.get("name") == active_name:
                    return b
        for b in backends:
            if b.get("enabled", True):
                return b
        return backends[0]


# ============================================================
# Flask App
# ============================================================
app = Flask(__name__, static_folder="static", template_folder="templates")

# Stable secret key (persists across restarts)
_secret_file = DATA_DIR / ".secret_key"
if _secret_file.exists():
    app.secret_key = _secret_file.read_text("utf-8").strip()
else:
    app.secret_key = os.urandom(24).hex()
    _secret_file.write_text(app.secret_key, encoding="utf-8")

# Session security
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # Prevent JS access to cookie
    SESSION_COOKIE_SAMESITE="Lax",   # CSRF mitigation
    PERMANENT_SESSION_LIFETIME=3600, # 1 hour session timeout
)

app.register_blueprint(auth_bp)


@app.context_processor
def inject_csrf_token():
    """Make CSRF token available in all templates."""
    return dict(csrf_token=generate_csrf_token())


_load_dotenv()
config = load_config()
llm = LLMClient(config)

# ============================================================
# Agent initialization (one-liner factory)
# ============================================================
agent_llm = AgentLLMClient(config)

# Apply memory settings from config.yaml (if present)
_memory_cfg = config.get("memory") or {}
_planning_cfg = config.get("planning") or {}
_retry_cfg = config.get("tool_retry") or {}
_agent_cfg = config.get("agent") or {}
_subagent_cfg = config.get("subagent") or {}
_mcp_cfg = config.get("mcp_servers") or {}
agent = create_agent(
    llm_client=agent_llm,
    db=db,
    workspace_dir=str(BASE_DIR),
    username="admin",
    user_id=1,
    memory_enabled=bool(_memory_cfg.get("enabled", True)),
    memory_auto_extract=bool(_memory_cfg.get("auto_extract", True)),
    memory_inject_count=int(_memory_cfg.get("inject_count", 4)),
    memory_inject_min_importance=int(_memory_cfg.get("inject_min_importance", 2)),
    memory_quality_gate=bool(_memory_cfg.get("quality_gate", True)),
    memory_consolidate=bool(_memory_cfg.get("consolidate_enabled", True)),
    planning_enabled=bool(_planning_cfg.get("enabled", True)),
    plan_min_user_chars=int(_planning_cfg.get("min_user_chars", 30)),
    plan_review_enabled=bool(_planning_cfg.get("review_enabled", True)),
    plan_review_every=int(_planning_cfg.get("review_every", 3)),
    plan_review_max_calls=int(_planning_cfg.get("review_max_calls", 4)),
    tool_retry_enabled=bool(_retry_cfg.get("enabled", True)),
    tool_retry_max=int(_retry_cfg.get("max_retries", 2)),
    tool_retry_base_delay=float(_retry_cfg.get("base_delay", 0.5)),
    execution_timeout=int(_agent_cfg.get("execution_timeout", 600)),
    max_tool_calls=int(_agent_cfg.get("max_tool_calls", 300)),
    context_window=int(_agent_cfg.get("context_window", 32000)),
    compression_enabled=bool(_agent_cfg.get("compression_enabled", True)),
    compression_trigger_ratio=float(_agent_cfg.get("compression_trigger_ratio", 0.75)),
    compression_min_messages=int(_agent_cfg.get("compression_min_messages", 10)),
    compression_keep_recent=int(_agent_cfg.get("compression_keep_recent", 6)),
    compression_min_gain_tokens=int(_agent_cfg.get("compression_min_gain_tokens", 800)),
    subagent_max_tool_calls=int(_subagent_cfg.get("max_tool_calls", 25)),
    subagent_max_duration=int(_subagent_cfg.get("max_duration", 300)),
    mcp_enabled=bool(_mcp_cfg or True),
    mcp_servers=_mcp_cfg,
    skills_dir=str(BASE_DIR / "skills"),
)

# Wire the module-level agent with mount support (mount_manager is defined
# after the approval manager below; attach it here so all code paths that
# reuse `agent` (e.g. /api/agent/chat) get mounted-folder access).
def _wire_mount(agent_obj):
    try:
        from agent.mount.manager import MountManager as _MM
        mm = globals().get("mount_manager")
        cb = globals().get("_mount_approval_cb")
        if mm is None or cb is None:
            return
        for tool in agent_obj.tools.all_tools():
            for attr in ("_mount_manager", "_approval_cb"):
                if hasattr(tool, attr):
                    try:
                        setattr(tool, attr, mm if attr == "_mount_manager" else cb)
                    except Exception:
                        pass
        # Rebuild the batch-read tool's inner single-reader too
        for tool in agent_obj.tools.all_tools():
            if hasattr(tool, "_single") and hasattr(tool._single, "_mount_manager"):
                try:
                    tool._single._mount_manager = mm
                    tool._single._approval_cb = cb
                except Exception:
                    pass
    except Exception:
        logger.exception("Failed to wire mount support into agent")

# ============================================================
# Observability: trace store + sink (persist every finished run)
# ============================================================
trace_store = TraceStore(DATA_DIR / "traces")

def _trace_sink(trace):
    """Persist a finished trace to the store (fail-soft)."""
    try:
        trace_store.save(trace)
    except Exception:
        logger.warning("Failed to persist trace %s", getattr(trace, "trace_id", "?"))

agent.trace_sink = _trace_sink

# Human-in-the-loop approval: shared manager so side-effectful tool calls
# (write_file / execute_code / delegate / memory_forget) pause for the user.
approval_manager = ApprovalManager(
    store=ApprovalStore(db),
    expiry_seconds=int((config.get("approval") or {}).get("expiry_seconds", 300)),
)
agent.approval_manager = approval_manager
# Approval is opt-in: enabled only when config says so.
agent.config.approval_enabled = bool((config.get("approval") or {}).get("enabled", True))
agent.config.approval_remember = bool((config.get("approval") or {}).get("remember", True))

# ============================================================
# Mounted folders (挂载目录): local folders the agent can access in place.
# Every access goes through the human-in-the-loop approval system.
# ============================================================
from agent.mount.manager import MountManager
mount_manager = MountManager(DATA_DIR)

# Per-run "allow" cache: run_id -> mount_id already approved in this run.
# Used only for mounts with policy="allow" (always_ask ignores it).
_mount_allow_cache: dict = {}

def _mount_approval_cb(tool_name: str, args: dict) -> dict | None:
    """Approval gate for mounted-folder access (called by the file tools).

    Policy per mount:
      - "always_ask": every access creates a fresh approval request.
      - "allow":      the first access in a run asks; once approved, further
                      accesses in the SAME run proceed without asking (the
                      per-run memory lives in the callback closure, keyed by
                      run_id; the orchestrator resets its own memory each run).

    Returns a ToolResult dict (blocked) or None (approved → proceed).
    """
    try:
        mount = mount_manager.get(args.get("mount_id") or "")
        if mount is None:
            return {
                "success": False,
                "error": "挂载不存在或已卸载，已阻止访问",
                "metadata": {"approval": "mount_missing"},
            }

        # Conversation scoping: a mount only works in the conversation it was
        # created for. Access from any other conversation (or a new one) is
        # blocked — the user must mount the folder again there.
        conv_id = args.get("conv_id")
        mount_conv = mount.get("conv_id") or ""
        if conv_id and mount_conv and conv_id != mount_conv:
            return {
                "success": False,
                "error": (
                    f"该挂载属于其他对话，不能在此对话中使用。"
                    f"请在本对话中重新挂载文件夹。"
                ),
                "metadata": {"approval": "mount_wrong_conversation"},
            }

        policy = mount.get("policy", "always_ask")
        run_id = args.get("run_id")

        # "allow" + already approved in this run → proceed silently.
        if policy == "allow" and run_id:
            if _mount_allow_cache.get(run_id) == mount["id"]:
                return None

        username = session.get("user", "admin")
        user = db.get_user(username)
        uid = user["id"] if user else None
        req = approval_manager.request(
            uid,
            f"{tool_name}:mount",
            {
                "mount_id": mount.get("id"),
                "path": args.get("path") or mount["path"],
                "operation": args.get("operation"),
                "mount_path": mount["path"],
                "提示": "Agent 请求访问挂载文件夹中的文件",
            },
        )
        status = approval_manager.wait_for_decision(
            req.get("id"), timeout=approval_manager.expiry_seconds,
            # A user cancellation must not wait out the full approval
            # expiry while the run is paused on a mount approval.
            should_stop=lambda: (
                run_id is not None
                and cancellation_manager.should_stop(run_id)
            ),
        )
        if status == "approved":
            # Remember the approval for the rest of this run (allow policy).
            if policy == "allow" and run_id:
                _mount_allow_cache[run_id] = mount["id"]
            return None
        return {
            "success": False,
            "error": f"用户未批准对挂载文件夹的访问: {args.get('path', '')}",
            "metadata": {"approval": status},
        }
    except Exception as e:
        logger.exception("Mount approval check failed")
        return {
            "success": False,
            "error": f"挂载访问审批异常: {e}，已阻止访问",
            "metadata": {"approval": "error"},
        }

# Wire the module-level agent (created above) with the mount manager and
# approval callback so code paths that reuse `agent` (e.g. /api/agent/chat)
# get mounted-folder access. Per-request agents pass them via create_agent.
_wire_mount(agent)

# Cancellation: shared manager so the front-end can stop a running agent.
_cancel_cfg = config.get("cancellation") or {}
agent.cancellation_manager = cancellation_manager
agent.config.cancellation_enabled = bool(_cancel_cfg.get("enabled", True))
agent.config.cancel_confirm_required = bool(_cancel_cfg.get("confirm_required", True))
agent.config.cancel_expiry = int(_cancel_cfg.get("expiry_seconds", 120))

# ============================================================
# Scheduled tasks (定时主动任务)
# ============================================================
_scheduled_cfg = config.get("scheduled") or {}
task_store = TaskStore(db)
scheduler = AgentScheduler(
    store=task_store,
    agent_factory=lambda: create_agent(
        llm_client=agent_llm,
        db=db,
        workspace_dir=str(BASE_DIR),
        username="admin",
        user_id=1,
        memory_enabled=bool(_memory_cfg.get("enabled", True)),
        memory_auto_extract=bool(_memory_cfg.get("auto_extract", True)),
        memory_inject_count=int(_memory_cfg.get("inject_count", 4)),
        memory_inject_min_importance=int(_memory_cfg.get("inject_min_importance", 2)),
        memory_quality_gate=bool(_memory_cfg.get("quality_gate", True)),
        memory_consolidate=bool(_memory_cfg.get("consolidate_enabled", True)),
        planning_enabled=bool(_planning_cfg.get("enabled", True)),
        plan_min_user_chars=int(_planning_cfg.get("min_user_chars", 30)),
        plan_review_enabled=bool(_planning_cfg.get("review_enabled", True)),
        plan_review_every=int(_planning_cfg.get("review_every", 3)),
        plan_review_max_calls=int(_planning_cfg.get("review_max_calls", 4)),
        tool_retry_enabled=bool(_retry_cfg.get("enabled", True)),
        tool_retry_max=int(_retry_cfg.get("max_retries", 2)),
        tool_retry_base_delay=float(_retry_cfg.get("base_delay", 0.5)),
        execution_timeout=int(_agent_cfg.get("execution_timeout", 600)),
        max_tool_calls=int(_agent_cfg.get("max_tool_calls", 300)),
        context_window=int(_agent_cfg.get("context_window", 32000)),
        compression_enabled=bool(_agent_cfg.get("compression_enabled", True)),
        compression_trigger_ratio=float(_agent_cfg.get("compression_trigger_ratio", 0.75)),
        compression_min_messages=int(_agent_cfg.get("compression_min_messages", 10)),
        compression_keep_recent=int(_agent_cfg.get("compression_keep_recent", 6)),
        compression_min_gain_tokens=int(_agent_cfg.get("compression_min_gain_tokens", 800)),
        subagent_max_tool_calls=int(_subagent_cfg.get("max_tool_calls", 25)),
        subagent_max_duration=int(_subagent_cfg.get("max_duration", 300)),
        mcp_enabled=bool(_mcp_cfg or True),
        mcp_servers=_mcp_cfg,
        mount_manager=mount_manager,
        mount_approval_cb=_mount_approval_cb,
        skills_dir=str(BASE_DIR / "skills"),
    ),
    rate_limiter=agent._rate_limiter,
    enabled=bool(_scheduled_cfg.get("enabled", True)),
)

def _start_scheduler():
    """Start the scheduled-task scheduler (called at server startup)."""
    try:
        scheduler.start()
    except Exception:
        logger.exception("Failed to start scheduled-task scheduler")
    return scheduler

# ============================================================
# Global error handlers
# ============================================================
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        error_logger.warning("404 Not Found: %s", request.path)
        return jsonify({"error": "Resource not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(e):
    error_logger.exception("500 Internal Server Error: %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return "Internal Server Error", 500


@app.errorhandler(405)
def method_not_allowed(e):
    error_logger.warning("405 Method Not Allowed: %s %s", request.method, request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Method not allowed"}), 405
    return "Method Not Allowed", 405


@app.before_request
def log_request():
    if not request.path.startswith("/static/"):
        request._start_time = time.time()
        request._request_id = uuid.uuid4().hex[:8]


@app.before_request
def csrf_protect():
    """Validate CSRF token on state-changing requests."""
    # Skip safe methods and login/logout
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.path in ("/login", "/logout"):
        return
    # Skip static files
    if request.path.startswith("/static/"):
        return
    # Validate token
    if not validate_csrf_token():
        logger.warning(
            "CSRF validation failed: %s %s ip=%s",
            request.method, request.path, request.remote_addr,
        )
        return jsonify({"error": "Invalid or missing CSRF token"}), 403


@app.after_request
def set_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    # SAMEORIGIN (not DENY): the main chat page embeds /traces?embed=1 in an
    # iframe (the always-visible Traces panel). Cross-origin embedding stays
    # blocked by the CSP below.
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    return response


@app.after_request
def log_response(response):
    if not request.path.startswith("/static/"):
        duration = time.time() - getattr(request, "_start_time", time.time())
        req_id = getattr(request, "_request_id", "-")
        user = session.get("user", "-")
        status = response.status_code

        # Record metrics
        metrics.record_request(request.path, status, duration)

        # Access log (all requests)
        access_logger.info(
            "[%s] %s %s %s %d %.1fms user=%s",
            req_id, request.remote_addr or "-",
            request.method, request.path, status,
            duration * 1000, user,
        )

        # Error log (4xx and 5xx)
        if status >= 400:
            error_logger.warning(
                "[%s] %s %s %d %.1fms user=%s",
                req_id, request.method, request.path, status,
                duration * 1000, user,
            )

    return response


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    """Get current configuration (without sensitive keys)"""
    try:
        cfg = load_config()
        safe_cfg = json.loads(json.dumps(cfg))
    except Exception as e:
        logger.exception("Failed to load config")
        return jsonify({"error": "Failed to load configuration"}), 500
    # Mask API keys (env-var placeholders resolve to real keys in memory —
    # never send those to the client; expose the env var name instead)
    for b in safe_cfg.get("llms", {}).get("backends", []):
        key = b.get("api_key") or ""
        env_name = b.pop("api_key_env", None)
        if env_name:
            b["api_key_env"] = env_name
            b["api_key_masked"] = f"${{{env_name}}}"
        elif key:
            b["api_key_masked"] = key[:8] + "****" + key[-4:] if len(key) > 12 else "****"
        b.pop("api_key", None)
    # Default active_backend to the first enabled backend if unset
    if not safe_cfg.get("active_backend"):
        backends = safe_cfg.get("llms", {}).get("backends", [])
        for b in backends:
            if b.get("enabled", True):
                safe_cfg["active_backend"] = b.get("name")
                break
    # Annotate each backend with its vision capability (for UI hints)
    for b in safe_cfg.get("llms", {}).get("backends", []):
        explicit = b.get("supports_vision")
        if explicit is not None:
            b["supports_vision"] = bool(explicit)
        else:
            model = (b.get("model") or "").lower()
            markers = (
                "vision", "v-l", "vl", "multimodal", "omni",
                "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "claude-3.5",
                "claude-3.7", "claude-sonnet-4", "claude-opus-4",
                "gemini", "qwen-vl", "qwen2.5-vl", "glm-4v",
                "internvl", "minicpm-v", "llava", "deepseek-vl",
            )
            b["supports_vision"] = any(m in model for m in markers)
    return jsonify(safe_cfg)


@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    """Update configuration (thread-safe)"""
    global config, llm, agent_llm
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    try:
        with _config_lock:
            config.update(data)
            save_config(config)
            # 重新解析 ${ENV} 占位符为真实 key,避免内存中残留字面占位符
            # (否则 LLM 请求会携带 "Bearer ${DEEPSEEK_API_KEY}" -> 401)
            config = _resolve_env_api_keys(config)
            llm = LLMClient(config)
            agent_llm.update_config(config)
    except Exception as e:
        logger.exception("Failed to update config")
        return jsonify({"error": f"Failed to save config: {str(e)}"}), 500
    return jsonify({"ok": True})


@app.route("/api/config/backend", methods=["POST"])
@login_required
def add_backend():
    """Add or update an LLM backend (thread-safe)"""
    global config, llm, agent_llm
    data = request.json
    if not data or not data.get("name"):
        return jsonify({"error": "Backend name is required"}), 400
    try:
        with _config_lock:
            backends = config.setdefault("llms", {}).setdefault("backends", [])

            # Find existing or add new
            existing = None
            for i, b in enumerate(backends):
                if b.get("name") == data.get("name"):
                    existing = i
                    break

            if existing is not None:
                backends[existing].update(data)
            else:
                backends.append(data)

            # New/updated api_key never lands in config.yaml as plaintext:
            # write it to .env (git-ignored) and store the ${ENV} reference.
            key = (data.get("api_key") or "").strip()
            if key and not key.startswith("${"):
                env_name = _env_name_for_backend(data["name"])
                _write_env_value(env_name, key)
                b = backends[existing] if existing is not None else backends[-1]
                b["api_key"] = f"${{{env_name}}}"
                b["api_key_env"] = env_name
                _env_api_keys[env_name] = key

            save_config(config)
            # 同上:add_backend 把 api_key 写成 ${ENV} 占位符后,内存必须重新
            # 解析成真实 key,否则后续 LLM 请求会携带字面占位符导致 401。
            config = _resolve_env_api_keys(config)
            llm = LLMClient(config)
            agent_llm.update_config(config)
    except Exception as e:
        logger.exception("Failed to save backend config")
        return jsonify({"error": f"Failed to save: {str(e)}"}), 500
    return jsonify({"ok": True})


@app.route("/api/config/backend/<name>", methods=["DELETE"])
@login_required
def delete_backend(name):
    """Delete an LLM backend (thread-safe)"""
    global config, llm, agent_llm
    with _config_lock:
        backends = config.get("llms", {}).get("backends", [])
        config["llms"]["backends"] = [b for b in backends if b.get("name") != name]
        save_config(config)
        llm = LLMClient(config)
        agent_llm.update_config(config)
    return jsonify({"ok": True})


@app.route("/api/models")
@login_required
def list_models():
    """List available models"""
    try:
        models = llm.list_models()
        return jsonify({"models": models})
    except Exception as e:
        logger.warning("Failed to list models: %s", e)
        return jsonify({"models": [], "error": str(e)}), 500


# ---- Conversation API ----

@app.route("/api/conversations")
@login_required
def api_conversations():
    try:
        return jsonify(get_conversations())
    except Exception as e:
        logger.exception("Failed to list conversations")
        return jsonify({"error": "Failed to load conversations", "conversations": []}), 500


@app.route("/api/conversations", methods=["POST"])
@login_required
def api_create_conversation():
    conv_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    data = {
        "id": conv_id,
        "title": "New Chat",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    try:
        save_conversation(conv_id, data)
    except Exception as e:
        logger.exception("Failed to create conversation")
        return jsonify({"error": "Failed to create conversation"}), 500
    return jsonify({"id": conv_id})


@app.route("/api/conversations/<conv_id>")
@login_required
def api_get_conversation(conv_id):
    data = load_conversation(conv_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    # Resolve stored attachment metadata to preview URLs (conversation files
    # may live in data/uploads/<conv_id>/ — serve them from there).
    # Mounted-folder references are NOT part of the conversation's files and
    # are filtered out of the attachment list.
    for msg in data.get("messages", []):
        atts = msg.get("attachments") or []
        if not atts:
            continue
        kept = []
        for a in atts:
            if (a or {}).get("kind") == "mount":
                continue  # mounted folders are not conversation files
            if not a.get("url"):
                fid = a.get("file_id") or ""
                ext = a.get("ext") or (os.path.splitext(a.get("name", ""))[1].lower())
                # Prefer the conversation the file was uploaded in, then the
                # viewed conversation, then the legacy flat root/_legacy.
                fp = _upload_path(fid, a.get("name", ""), a.get("conv_id") or conv_id)
                if fp:
                    parts = fp.relative_to(UPLOAD_DIR).parts
                    if len(parts) == 2:
                        a["url"] = f"/api/uploads/{parts[0]}/{parts[1]}"
                    else:
                        a["url"] = f"/api/uploads/{fp.name}"
            kept.append(a)
        msg["attachments"] = kept
    # Conversation file folder: data/uploads/<conv_id>/ — the files this
    # conversation uploaded (images/texts/docs), browsable from history.
    data["files"] = _list_conversation_files(conv_id)
    return jsonify(data)


def _list_conversation_files(conv_id: str, max_items: int = 100) -> list[dict]:
    """List files stored in this conversation's upload folder."""
    folder = UPLOAD_DIR / conv_id
    if not folder.is_dir():
        return []
    items = []
    try:
        for fp in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not fp.is_file():
                continue
            ext = fp.suffix.lower()
            kind = "image" if ext in _IMAGE_EXTS else ("text" if ext in _TEXT_EXTS else "doc")
            items.append({
                "file_id": fp.stem,
                "name": fp.name,
                "ext": ext,
                "kind": kind,
                "size": fp.stat().st_size,
                "url": f"/api/uploads/{conv_id}/{fp.name}",
            })
            if len(items) >= max_items:
                break
    except Exception as e:
        logger.warning("Failed to list conversation files %s: %s", conv_id, e)
    return items


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
@login_required
def api_delete_conversation(conv_id):
    try:
        delete_conversation(conv_id)
    except Exception as e:
        logger.exception("Failed to delete conversation %s", conv_id)
        return jsonify({"error": "Failed to delete conversation"}), 500
    return jsonify({"ok": True})


@app.route("/api/conversations/<conv_id>/title", methods=["PUT"])
@login_required
def api_update_title(conv_id):
    data = load_conversation(conv_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    new_title = request.json.get("title", "").strip() if request.json else ""
    if not new_title:
        return jsonify({"error": "Title cannot be empty"}), 400
    if len(new_title) > 100:
        return jsonify({"error": "Title too long (max 100 characters)"}), 400
    try:
        # Update ONLY the title — never touch the messages table (a full
        # save_conversation would delete/re-insert all messages and drop
        # their attachments metadata).
        db.update_conversation(conv_id, title=new_title)
    except Exception as e:
        logger.exception("Failed to update title for %s", conv_id)
        return jsonify({"error": "Failed to save"}), 500
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """Send a message and get AI response (uses agent with tool support)"""
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "").strip()
    run_id = (data.get("run_id") or "").strip() or None
    attachments = data.get("attachments") or []

    if not user_message and not attachments:
        return jsonify({"error": "Empty message"}), 400

    if len(user_message) > 10000:
        return jsonify({"error": "Message too long (max 10,000 characters)"}), 400

    # Load or create conversation
    conv = load_conversation(conv_id) if conv_id else None
    if not conv:
        conv_id = uuid.uuid4().hex[:12]
        title = user_message[:40]
        db.create_conversation(conv_id, title=title)
        conv = {"id": conv_id, "title": title, "messages": []}
        metrics.record_conversation_created()

    # Build the effective message from text + attachments:
    # images become base64 data URLs (multimodal), other files get a path
    # description so the agent can read them with read_file tools, and
    # mounted folders get a full manifest so the agent can read any file.
    attach_lines = []
    _vision_ok = bool(agent_llm.supports_vision)
    _has_image_att = False
    for att in attachments[:8]:
        fid = (att.get("file_id") or "").strip()
        name = att.get("name") or fid
        if not fid:
            continue
        if att.get("kind") == "mount":
            manifest = mount_manager.manifest(fid)
            if manifest:
                attach_lines.append(manifest)
            else:
                attach_lines.append(f"[挂载文件夹] {name} (路径无效或为空)")
            continue
        img = _attachment_image_url(fid, name, conv_id)
        if img:
            _has_image_att = True
            attach_lines.append(f"[图片] {name}")
        else:
            ext = os.path.splitext(name)[1].lower()
            txt = _attachment_text(fid, name, conv_id)
            if txt:
                attach_lines.append(f"[文件] {name}:\n{txt}")
            else:
                fp = UPLOAD_DIR / f"{fid}{ext}"
                attach_lines.append(f"[文件] {name} (路径: {fp})")
    effective_message = user_message
    image_parts = []
    for att in attachments[:8]:
        fid = (att.get("file_id") or "").strip()
        name = att.get("name") or fid
        if not fid:
            continue
        if att.get("kind") == "mount":
            image_parts.append({
                "file_id": fid,
                "name": name,
                "kind": "mount",
                "ext": "",
            })
            continue
        img = _attachment_image_url(fid, name, conv_id)
        if img:
            image_parts.append({
                "image_url": img,
                "file_id": fid,
                "name": name,
                "kind": "image",
                "ext": os.path.splitext(name)[1].lower(),
                "conv_id": conv_id,
            })
        else:
            ext = os.path.splitext(name)[1].lower()
            kind = "image" if ext in _IMAGE_EXTS else ("text" if ext in _TEXT_EXTS else "doc")
            image_parts.append({
                "file_id": fid,
                "name": name,
                "kind": kind,
                "ext": ext,
                "conv_id": conv_id,
            })
    if attach_lines:
        block = "\n".join(attach_lines)
        effective_message = (
            f"{block}\n\n{user_message}" if user_message else block
        )

    # Short-circuit: the current model can't process images — don't call the
    # LLM (text-only endpoints 404 on image_url parts). Return a clear notice.
    if _has_image_att and not _vision_ok:
        logger.info(
            "Active model '%s' has no vision support — refusing image request",
            getattr(agent_llm, "model_name", ""),
        )
        db.add_message(conv_id, "user", user_message,
                       attachments=image_parts or None)
        db.update_conversation(conv_id)
        return jsonify({
            "conversation_id": conv_id,
            "content": "当前大模型不支持多模态，无法查看图片。请切换到支持图片输入的模型后再试。",
            "title": conv["title"],
            "cancelled": False,
            "vision_blocked": True,
        })

    # Get current username from session
    username = session.get("user", "admin")

    # Recreate agent with current username for permission checks
    current_user = db.get_user(username)
    current_agent = create_agent(
        llm_client=agent_llm,
        db=db,
        workspace_dir=str(BASE_DIR),
        username=username,
        user_id=current_user["id"] if current_user else None,
        mount_manager=mount_manager,
        mount_approval_cb=_mount_approval_cb,
        skills_dir=str(BASE_DIR / "skills"),
    )
    current_agent.trace_sink = _trace_sink
    current_agent.approval_manager = approval_manager
    current_agent.config.approval_enabled = agent.config.approval_enabled
    current_agent.config.approval_remember = agent.config.approval_remember
    current_agent.cancellation_manager = cancellation_manager
    current_agent.config.cancellation_enabled = agent.config.cancellation_enabled
    current_agent.config.cancel_confirm_required = agent.config.cancel_confirm_required
    current_agent.config.cancel_expiry = agent.config.cancel_expiry

    # Run agent. user_message passed to the agent carries the generated
    # attachment/mount descriptions, but the conversation history must keep
    # the user's raw input — persist_message does that.
    llm_start = time.time()
    try:
        result = current_agent.run(
            effective_message, conversation_id=conv_id, run_id=run_id,
            user_attachments=image_parts or None,
            persist_message=user_message,
        )
        llm_duration = time.time() - llm_start
        metrics.record_llm_call(
            model="agent",
            duration=llm_duration,
            tokens_used=0,
        )
        metrics.record_chat_message()
    except Exception as e:
        logger.exception("Agent execution failed")
        return jsonify({"error": f"Agent error: {str(e)}"}), 500

    # Update conversation title if first exchange
    messages = db.get_messages(conv_id)
    if len(messages) <= 2:
        db.update_conversation(conv_id, title=user_message[:40])
        conv["title"] = user_message[:40]

    db.update_conversation(conv_id)

    return jsonify({
        "conversation_id": conv_id,
        "content": result["content"],
        "reasoning": result.get("reasoning", ""),
        "title": conv["title"],
        "cancelled": bool(result.get("cancelled")),
    })


# ---- File / image upload API ----

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
_TEXT_EXTS = {".txt", ".md", ".py", ".js", ".json", ".csv", ".yaml", ".yml", ".html", ".css", ".log"}
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}


def _safe_filename(name: str) -> str:
    """Keep only the basename and strip dangerous chars."""
    import ntpath
    base = ntpath.basename(name.replace("\\", "/"))
    return "".join(c for c in base if c.isalnum() or c in "._- ").strip() or "file"


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    """Upload a file/image. Returns its id and metadata.

    When a `conv_id` form field is present the file is stored under
    data/uploads/<conv_id>/<file_id><ext> so each conversation's files live
    in their own folder (viewable when browsing the conversation). Without a
    conv_id the file lands in data/uploads/ (legacy flat layout).
    """
    if "file" not in request.files:
        return jsonify({"error": "缺少文件字段 (file)"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "空文件"}), 400

    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size == 0:
        return jsonify({"error": "文件为空"}), 400
    if size > MAX_UPLOAD_SIZE:
        return jsonify({"error": f"文件过大 (最大 {MAX_UPLOAD_SIZE // (1024*1024)}MB)"}), 413

    file_id = uuid.uuid4().hex[:16]
    name = _safe_filename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    mime = f.mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    kind = "image" if ext in _IMAGE_EXTS else ("text" if ext in _TEXT_EXTS else "doc")

    # Conversation-scoped uploads: data/uploads/<conv_id>/<file_id><ext>
    conv_id = (request.form.get("conv_id") or "").strip()
    dest_dir = UPLOAD_DIR
    if conv_id and _validate_conv_id(conv_id):
        dest_dir = UPLOAD_DIR / conv_id
        dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{file_id}{ext}"
    f.save(str(dest))

    return jsonify({
        "ok": True,
        "file_id": file_id,
        "name": name,
        "size": size,
        "mime": mime,
        "kind": kind,
        "ext": ext,
        "conv_id": conv_id if conv_id and _validate_conv_id(conv_id) else None,
        "url": f"/api/uploads/{file_id}{ext}",
    }), 201


@app.route("/api/uploads/<path:fname>", methods=["GET"])
@login_required
def api_uploaded_file(fname):
    """Serve an uploaded file for preview.

    fname looks like <file_id><ext> (legacy flat layout, also served from
    _legacy/ after migration) or <conv_id>/<file_id><ext> (conversation
    folders)."""
    # Prevent path traversal
    if ".." in fname or "\\" in fname:
        return jsonify({"error": "Invalid path"}), 400
    parts = fname.split("/")
    if len(parts) == 2:
        conv_id, fid = parts
        if not _validate_conv_id(conv_id):
            return jsonify({"error": "Invalid path"}), 400
        safe_fid = _safe_filename(fid)
        fp = UPLOAD_DIR / conv_id / safe_fid
        if fp.is_file():
            return send_from_directory(str(UPLOAD_DIR / conv_id), safe_fid)
        return jsonify({"error": "文件不存在"}), 404
    if len(parts) == 1:
        safe = _safe_filename(fname)
        for root in (UPLOAD_DIR, UPLOAD_DIR / "_legacy"):
            fp = root / safe
            if fp.is_file():
                return send_from_directory(str(root), safe)
        return jsonify({"error": "文件不存在"}), 404
    return jsonify({"error": "Invalid path"}), 400


@app.route("/api/uploads/<path:fname>", methods=["DELETE"])
@login_required
def api_delete_upload(fname):
    """Delete an uploaded file (fname = <conv_id>/<file_id><ext>).

    Called when the user removes a pending upload from the composer — the
    file must not linger in the conversation's file list.
    """
    if ".." in fname or "\\" in fname:
        return jsonify({"error": "Invalid path"}), 400
    parts = fname.split("/")
    if len(parts) == 2:
        conv_id, fid = parts
        if not _validate_conv_id(conv_id):
            return jsonify({"error": "Invalid path"}), 400
        safe_fid = _safe_filename(fid)
        fp = UPLOAD_DIR / conv_id / safe_fid
    elif len(parts) == 1:
        safe = _safe_filename(fname)
        fp = UPLOAD_DIR / safe
    else:
        return jsonify({"error": "Invalid path"}), 400
    try:
        if fp.is_file():
            fp.unlink()
            return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"error": f"删除失败: {e}"}), 500
    return jsonify({"error": "文件不存在"}), 404


def _upload_path(file_id: str, name: str = "", conv_id: str | None = None):
    """Resolve an uploaded file's path, searching conversation folders first.

    New uploads live under data/uploads/<conv_id>/, legacy ones in the flat
    data/uploads/ root or data/uploads/_legacy/ (archived flat files).
    `name` supplies the extension when the stored file is only identifiable
    via its metadata (base file_id alone has none).

    Lookup is O(1) path construction — never scans the (possibly huge)
    _legacy archive directory.
    """
    if not file_id:
        return None
    ext = os.path.splitext(name or "")[1].lower()
    if conv_id and _validate_conv_id(conv_id):
        p = UPLOAD_DIR / conv_id / f"{file_id}{ext}"
        if p.is_file():
            return p
    p = UPLOAD_DIR / f"{file_id}{ext}"
    if p.is_file():
        return p
    p = UPLOAD_DIR / "_legacy" / f"{file_id}{ext}"
    if p.is_file():
        return p
    # No extension derived (metadata missing): fall back to scanning the
    # conversation folder only (small); skip the legacy archive entirely.
    if conv_id and _validate_conv_id(conv_id):
        folder = UPLOAD_DIR / conv_id
        if folder.is_dir():
            for fp in folder.iterdir():
                if fp.is_file() and fp.stem == file_id:
                    return fp
    return None


def _attachment_text(file_id: str, name: str, conv_id: str | None = None) -> str | None:
    """Read an uploaded file's text content (small text files only)."""
    try:
        ext = os.path.splitext(name)[1].lower()
        if ext not in _TEXT_EXTS:
            return None
        fp = _upload_path(file_id, name, conv_id)
        if not fp:
            return None
        data = fp.read_bytes()[:10000].decode("utf-8", errors="replace")
        return data
    except Exception:
        return None


def _attachment_image_url(file_id: str, name: str, conv_id: str | None = None) -> str | None:
    """Return a base64 data URL for an image upload (or None)."""
    try:
        ext = os.path.splitext(name)[1].lower()
        if ext not in _IMAGE_EXTS:
            return None
        fp = _upload_path(file_id, name, conv_id)
        if not fp:
            return None
        data = fp.read_bytes()
        mime = mimetypes.guess_type(name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:
        return None


# ---- Agent API ----

@app.route("/api/agent/chat", methods=["POST"])
@login_required
def api_agent_chat():
    """Agent chat endpoint - supports tool use and multi-step reasoning."""
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "").strip()
    run_id = (data.get("run_id") or "").strip() or None
    attachments = data.get("attachments") or []

    if not user_message and not attachments:
        return jsonify({"error": "Empty message"}), 400

    if len(user_message) > 10000:
        return jsonify({"error": "Message too long (max 10,000 characters)"}), 400

    # Load or create conversation
    conv = load_conversation(conv_id) if conv_id else None
    if not conv:
        conv_id = uuid.uuid4().hex[:12]
        title = user_message[:40]
        db.create_conversation(conv_id, title=title)
        conv = {"id": conv_id, "title": title, "messages": []}
        metrics.record_conversation_created()

    # Run agent
    llm_start = time.time()
    try:
        result = agent.run(user_message, conversation_id=conv_id)
        llm_duration = time.time() - llm_start

        metrics.record_llm_call(
            model=agent.config.system_prompt[:20],
            duration=llm_duration,
            tokens_used=0,
        )
        metrics.record_chat_message()
    except Exception as e:
        logger.exception("Agent execution failed")
        return jsonify({"error": f"Agent error: {str(e)}"}), 500

    # Update conversation title if first exchange
    messages = db.get_messages(conv_id)
    if len(messages) <= 2:
        db.update_conversation(conv_id, title=user_message[:40])
        conv["title"] = user_message[:40]

    db.update_conversation(conv_id)

    return jsonify({
        "conversation_id": conv_id,
        "content": result["content"],
        "reasoning": result.get("reasoning", ""),
        "title": conv["title"],
        "tool_calls_made": result["tool_calls_made"],
        "iterations": result["iterations"],
    })


@app.route("/api/agent/stream", methods=["POST"])
@login_required
def api_agent_stream():
    """Agent SSE streaming endpoint - real-time tool call events."""
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "").strip()
    run_id = (data.get("run_id") or "").strip() or None
    attachments = data.get("attachments") or []

    if not user_message and not attachments:
        return jsonify({"error": "Empty message"}), 400

    # Build multimodal attachment parts (same as /api/chat)
    _stream_vision_ok = bool(agent_llm.supports_vision)
    _stream_has_image = False
    stream_att_parts = []
    for att in attachments[:8]:
        fid = (att.get("file_id") or "").strip()
        name = att.get("name") or fid
        if not fid:
            continue
        if att.get("kind") == "mount":
            stream_att_parts.append({
                "file_id": fid, "name": name, "kind": "mount", "ext": "",
            })
            continue
        img = _attachment_image_url(fid, name, conv_id)
        if img:
            _stream_has_image = True
            if _stream_vision_ok:
                stream_att_parts.append({
                    "image_url": img, "file_id": fid, "name": name, "kind": "image",
                    "ext": os.path.splitext(name)[1].lower(), "conv_id": conv_id,
                })
            else:
                stream_att_parts.append({
                    "file_id": fid, "name": name, "kind": "image",
                    "ext": os.path.splitext(name)[1].lower(), "conv_id": conv_id,
                })
        else:
            ext = os.path.splitext(name)[1].lower()
            kind = "image" if ext in _IMAGE_EXTS else ("text" if ext in _TEXT_EXTS else "doc")
            stream_att_parts.append({"file_id": fid, "name": name, "kind": kind, "ext": ext, "conv_id": conv_id})

    # Keep the user's raw input for history persistence (user_message below
    # gets overwritten with the generated attachment/mount descriptions).
    _raw_user_message = user_message

    # Effective message: append attachment description lines
    stream_attach_lines = []
    for att in attachments[:8]:
        fid = (att.get("file_id") or "").strip()
        name = att.get("name") or fid
        if not fid:
            continue
        if att.get("kind") == "mount":
            manifest = mount_manager.manifest(fid)
            if manifest:
                stream_attach_lines.append(manifest)
            else:
                stream_attach_lines.append(f"[挂载文件夹] {name} (路径无效或为空)")
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in _IMAGE_EXTS:
            stream_attach_lines.append(f"[图片] {name}")
        else:
            txt = _attachment_text(fid, name, conv_id)
            if txt:
                stream_attach_lines.append(f"[文件] {name}:\n{txt}")
            else:
                stream_attach_lines.append(f"[文件] {name} (路径: {UPLOAD_DIR / (fid + ext)})")
    if stream_attach_lines:
        block = "\n".join(stream_attach_lines)
        user_message = f"{block}\n\n{user_message}" if user_message else block

    # Load or create conversation
    conv = load_conversation(conv_id) if conv_id else None
    if not conv:
        conv_id = uuid.uuid4().hex[:12]
        title = _raw_user_message[:40]
        db.create_conversation(conv_id, title=title)
        conv = {"id": conv_id, "title": title, "messages": []}
        metrics.record_conversation_created()

    # Short-circuit: no vision model can't process images — no LLM call.
    if _stream_has_image and not _stream_vision_ok:
        logger.info(
            "Active model '%s' has no vision support — refusing image request (stream)",
            getattr(agent_llm, "model_name", ""),
        )
        db.add_message(conv_id, "user", _raw_user_message,
                       attachments=stream_att_parts or None)
        db.update_conversation(conv_id)

        def _vision_blocked_stream():
            yield f"data: {json.dumps({'type': 'text', 'content': '当前大模型不支持多模态，无法查看图片。请切换到支持图片输入的模型后再试。', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
        return Response(
            _vision_blocked_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def generate():
        try:
            # Emit the conversation id FIRST — a new conversation is created
            # before the stream starts, and long approval waits (up to the
            # expiry) must not prevent the front-end from adopting the id.
            yield f"data: {json.dumps({'type': 'start', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            for event in agent.run_stream(
                user_message, conversation_id=conv_id, run_id=run_id,
                user_attachments=stream_att_parts or None,
                persist_message=_raw_user_message,
            ):
                # Attach the conversation id to terminal events so the
                # front-end can adopt the conversation (first message of a
                # new conversation has no conv_id up front).
                if event.get("type") in ("done", "cancelled", "error"):
                    event = dict(event)
                    event["conversation_id"] = conv_id
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("Agent stream failed")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            # Update conversation title if first exchange
            try:
                messages = db.get_messages(conv_id)
                if len(messages) <= 2:
                    db.update_conversation(conv_id, title=_raw_user_message[:40])
                db.update_conversation(conv_id)
                metrics.record_chat_message()
            except Exception:
                pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/agent/tools", methods=["GET"])
@login_required
def api_agent_tools():
    """List available agent tools."""
    tools_info = []
    for tool in agent.tools.all_tools():
        tools_info.append({
            "name": tool.name,
            "description": tool.description,
        })
    return jsonify({"tools": tools_info})


# ---- Mounted folders API (挂载目录) ----

@app.route("/api/mounts", methods=["GET"])
@login_required
def api_mounts_list():
    """List mounted folders. Scoped to the current conversation: ?conv_id=...
    returns only the mounts attached to that conversation (mounts are
    conversation-scoped — every conversation mounts its own folders)."""
    conv_id = request.args.get("conv_id") or None
    mounts = mount_manager.list(conv_id)
    # Attach the owning conversation name for UI display
    result = []
    for m in mounts:
        m = dict(m)
        if m.get("conv_id"):
            conv = db.get_conversation(m["conv_id"])
            m["conv_title"] = conv["title"] if conv else m["conv_id"]
        result.append(m)
    return jsonify({"mounts": result})


# ---- Skills API (技能) ----

SKILLS_DIR = BASE_DIR / "skills"

def _list_skills() -> list[dict]:
    """List available skills: one entry per folder containing a SKILL.md."""
    if not SKILLS_DIR.is_dir():
        return []
    skills = []
    for folder in sorted(SKILLS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            continue
        name = folder.name
        description = ""
        try:
            head = skill_file.read_text(encoding="utf-8", errors="replace")[:300]
            for line in head.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:120]
                    break
        except Exception:
            pass
        skills.append({"name": name, "description": description})
    return skills


@app.route("/api/skills", methods=["GET"])
@login_required
def api_skills_list():
    """List available skills (from the skills/ folder)."""
    return jsonify({"skills": _list_skills()})


@app.route("/api/skills/<path:skill_name>/content", methods=["GET"])
@login_required
def api_skills_content(skill_name):
    """Return the SKILL.md content of one skill."""
    from werkzeug.utils import safe_join
    base = SKILLS_DIR.resolve()
    joined = safe_join(str(base), skill_name, "SKILL.md")
    if not joined:
        return jsonify({"error": "Skill not found"}), 404
    skill_file = Path(joined)
    if not skill_file.is_file():
        return jsonify({"error": "Skill not found"}), 404
    try:
        content = skill_file.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return jsonify({"error": f"Read failed: {e}"}), 500
    return jsonify({"name": skill_name, "content": content})


@app.route("/api/conversations/<conv_id>/skills", methods=["PUT"])
@login_required
def api_conversation_skills(conv_id):
    """Set the skills attached to a conversation. Body: {"skills": [...]}"""
    data = request.get_json(silent=True) or {}
    skills = data.get("skills") or []
    # Only keep skill names that actually exist
    available = {s["name"] for s in _list_skills()}
    skills = [s for s in skills if s in available]
    db.set_conversation_skills(conv_id, skills)
    return jsonify({"ok": True, "skills": skills})


@app.route("/api/mounts", methods=["POST"])
@login_required
def api_mounts_create():
    """Mount a local folder by absolute path.

    Body: {"path": "...", "policy": "always_ask" | "allow", "conv_id": "..."}
      - always_ask (default): ask the human before EVERY access.
      - allow: ask on first access per run, then proceed within the run.
      - conv_id: REQUIRED — mounts are conversation-scoped (they only work
        in their own conversation), so a mount without a conversation would
        be an orphan nobody can use.
    """
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip() or None
    policy = (data.get("policy") or "always_ask").strip()
    conv_id = data.get("conv_id") or None
    if not conv_id:
        return jsonify({"error": "请先选择或新建对话，再挂载文件夹"}), 400
    if not _validate_conv_id(conv_id):
        return jsonify({"error": "Invalid conversation id"}), 400
    if db.get_conversation(conv_id) is None:
        return jsonify({"error": "对话不存在，请先新建或选择对话"}), 400
    mount, err = mount_manager.mount(path, name, policy=policy, conv_id=conv_id)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "mount": mount}), 201


@app.route("/api/mounts/<mount_id>/policy", methods=["PUT"])
@login_required
def api_mounts_policy(mount_id):
    """Update a mount's access policy (always_ask / allow)."""
    data = request.get_json(silent=True) or {}
    policy = (data.get("policy") or "").strip()
    ok = mount_manager.set_policy(mount_id, policy)
    if not ok:
        return jsonify({"error": "挂载不存在或策略无效"}), 400
    return jsonify({"ok": True, "policy": policy})


@app.route("/api/mounts/<mount_id>", methods=["DELETE"])
@login_required
def api_mounts_delete(mount_id):
    """Unmount a folder."""
    ok = mount_manager.unmount(mount_id)
    if not ok:
        return jsonify({"error": "挂载不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/mounts/<mount_id>/manifest", methods=["GET"])
@login_required
def api_mounts_manifest(mount_id):
    """Get the file manifest of a mounted folder."""
    manifest = mount_manager.manifest(mount_id)
    if manifest is None:
        return jsonify({"error": "挂载不存在或为空"}), 404
    return jsonify({"ok": True, "manifest": manifest})


# ---- Human-in-the-loop approval API ----

def _approval_user_id():
    """Current session user's db id (None if unknown)."""
    username = session.get("user", "admin")
    user = db.get_user(username)
    return user["id"] if user else None


@app.route("/api/approvals", methods=["GET"])
@login_required
def api_approvals():
    """List pending approval requests for the current user."""
    uid = _approval_user_id()
    pending = approval_manager.pending(user_id=uid)
    return jsonify({"pending": pending})


@app.route("/api/approvals/<int:req_id>/approve", methods=["POST"])
@login_required
def api_approval_approve(req_id):
    uid = _approval_user_id()
    ok, msg = approval_manager.approve(req_id, user_id=uid)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/approvals/<int:req_id>/reject", methods=["POST"])
@login_required
def api_approval_reject(req_id):
    uid = _approval_user_id()
    data = request.json or {}
    reason = data.get("reason") or "用户拒绝"
    ok, msg = approval_manager.reject(req_id, user_id=uid, reason=reason)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


# ---- Cancellation API (stop a running agent) ----

def _is_task_mode_message(message: str) -> bool:
    """Roughly mirrors the planner's trigger: long messages or action cues
    enter "task mode", where cancellation requires a confirm step."""
    if not message:
        return False
    if len(message) >= 30:
        return True
    cue_words = ("写", "创建", "制作", "分析", "研究", "整理", "生成", "开发", "调查",
                 "搜索", "检查", "修复", "部署", "总结", "对比", "设计", "实现")
    return any(w in message for w in cue_words)


@app.route("/api/cancel", methods=["POST"])
@login_required
def api_cancel():
    """Request cancellation of a running agent.

    Body: {"run_id": "...", "mode": "direct" | "confirm"}
      - direct: abort immediately (plain chat)
      - confirm: enter pending state; the front-end then asks the user and
        calls /api/cancel/<run_id>/approve or /deny
    """
    data = request.json or {}
    run_id = (data.get("run_id") or "").strip()
    if not run_id:
        return jsonify({"error": "Missing run_id"}), 400
    mode = data.get("mode") or DIRECT
    if mode not in (DIRECT, CONFIRM):
        mode = DIRECT
    req = cancellation_manager.request(
        run_id, mode=mode,
        expiry_seconds=float(agent.config.cancel_expiry or 120),
    )
    return jsonify({
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        "status": req.status,
    })


@app.route("/api/cancel/<run_id>/approve", methods=["POST"])
@login_required
def api_cancel_approve(run_id):
    """Approve a pending cancellation -> the running agent stops."""
    ok = cancellation_manager.approve(run_id)
    return jsonify({"ok": ok, "message": "已确认取消" if ok else "取消失败（可能已过期）"}), (200 if ok else 400)


@app.route("/api/cancel/<run_id>/deny", methods=["POST"])
@login_required
def api_cancel_deny(run_id):
    """Deny a pending cancellation -> the agent keeps running."""
    data = request.json or {}
    ok = cancellation_manager.deny(run_id, reason=data.get("reason") or "")
    return jsonify({"ok": ok, "message": "已继续执行" if ok else "操作失败"}), (200 if ok else 400)


@app.route("/api/cancel/<run_id>", methods=["GET"])
@login_required
def api_cancel_status(run_id):
    """Check a cancellation request's status (for the front-end poll)."""
    req = cancellation_manager.get(run_id)
    if req is None:
        return jsonify({"ok": False, "status": "none"}), 404
    return jsonify({
        "ok": True,
        "run_id": run_id,
        "status": req.status,
        "mode": req.mode,
    })


# ---- Long-term memory API ----

def _memory_service_for_current_user():
    """Build a MemoryService scoped to the current session user."""
    username = session.get("user", "admin")
    user = db.get_user(username)
    return MemoryService(db, user_id=user["id"] if user else None)


@app.route("/api/agent/memories", methods=["GET"])
@login_required
def api_agent_memories():
    """List the current user's long-term memories."""
    service = _memory_service_for_current_user()
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    mtype = request.args.get("memory_type") or None
    memories = service.list(limit=limit, memory_type=mtype)
    return jsonify({
        "memories": [
            {
                "id": m.get("id"),
                "memory_type": m.get("memory_type"),
                "content": m.get("content"),
                "importance": m.get("importance"),
                "conversation_id": m.get("conversation_id"),
                "created_at": m.get("created_at"),
                "updated_at": m.get("updated_at"),
                "last_accessed_at": m.get("last_accessed_at"),
            }
            for m in memories
        ],
        "count": len(memories),
    })


@app.route("/api/agent/memories", methods=["POST"])
@login_required
def api_agent_memories_store():
    """Store a long-term memory for the current user."""
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400

    service = _memory_service_for_current_user()
    result = service.store(
        content=content,
        memory_type=data.get("memory_type", "fact"),
        importance=data.get("importance", 3),
        conversation_id=data.get("conversation_id"),
    )
    if result.get("stored"):
        return jsonify({"ok": True, "memory": result["memory"]}), 201
    return jsonify({
        "ok": False,
        "duplicate_of": result.get("duplicate_of"),
        "memory": result.get("memory"),
    }), 200


@app.route("/api/agent/memories/<int:mem_id>", methods=["DELETE"])
@login_required
def api_agent_memories_delete(mem_id):
    """Delete a long-term memory."""
    service = _memory_service_for_current_user()
    deleted = service.forget(mem_id)
    if deleted:
        return jsonify({"ok": True})
    return jsonify({"error": "Memory not found"}), 404


@app.route("/api/agent/memories/search", methods=["GET"])
@login_required
def api_agent_memories_search():
    """Search the current user's long-term memories."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "q is required"}), 400
    service = _memory_service_for_current_user()
    results = service.search(
        query=query,
        limit=min(max(int(request.args.get("limit", 10)), 1), 50),
        memory_type=request.args.get("memory_type") or None,
    )
    return jsonify({"results": results, "count": len(results)})


@app.route("/api/agent/config", methods=["GET"])
@login_required
def api_agent_config():
    """Get current agent configuration."""
    cfg = agent.config
    return jsonify({
        "max_iterations": cfg.max_iterations,
        "temperature": cfg.temperature,
        "context_window": cfg.context_window,
        "tools_enabled": cfg.tools_enabled,
        "tool_choice": cfg.tool_choice,
    })


@app.route("/api/agent/config", methods=["POST"])
@login_required
def api_agent_config_update():
    """Update agent configuration."""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    try:
        agent.set_config(**data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- Agent Observability (traces) ----

@app.route("/traces")
@login_required
def traces_page():
    """Agent run replay page. ?embed=1 renders without the back link
    (used by the always-visible Traces panel in the main chat UI)."""
    embed = bool(request.args.get("embed"))
    return render_template("traces.html", embed=embed)


# ---- Scheduled tasks (定时主动任务) ----

@app.route("/scheduled")
@login_required
def scheduled_page():
    """Scheduled tasks management page."""
    return render_template("scheduled.html")


@app.route("/api/scheduled/tasks", methods=["GET"])
@login_required
def api_scheduled_tasks():
    """List all scheduled tasks."""
    tasks = task_store.list_tasks()
    return jsonify({"tasks": tasks})


@app.route("/api/scheduled/tasks", methods=["POST"])
@login_required
def api_scheduled_create():
    """Create a scheduled task."""
    data = request.json or {}
    name = str(data.get("name", "")).strip()
    prompt = str(data.get("prompt", "")).strip()
    schedule = str(data.get("schedule", "")).strip()
    enabled = bool(data.get("enabled", True))
    conversation_id = data.get("conversation_id") or None

    if not name or not prompt or not schedule:
        return jsonify({"error": "name/prompt/schedule 均为必填"}), 400
    ok, msg = scheduler.validate_schedule(schedule)
    if not ok:
        return jsonify({"error": f"cron 表达式无效: {msg}"}), 400

    task_id = task_store.create_task(
        name, prompt, schedule, enabled=enabled, conversation_id=conversation_id
    )
    task = task_store.get_task(task_id)
    if task and task.get("enabled"):
        scheduler.refresh_task(task_id)
    return jsonify({"task": task}), 201


@app.route("/api/scheduled/tasks/<int:task_id>", methods=["PUT"])
@login_required
def api_scheduled_update(task_id):
    """Update a scheduled task (name/prompt/schedule/enabled)."""
    data = request.json or {}
    fields = {}
    for key in ("name", "prompt", "schedule", "enabled", "conversation_id"):
        if key in data:
            fields[key] = data[key]
    if "schedule" in fields:
        ok, msg = scheduler.validate_schedule(str(fields["schedule"]))
        if not ok:
            return jsonify({"error": f"cron 表达式无效: {msg}"}), 400
    task_store.update_task(task_id, **fields)
    scheduler.refresh_task(task_id)
    return jsonify({"task": task_store.get_task(task_id)})


@app.route("/api/scheduled/tasks/<int:task_id>", methods=["DELETE"])
@login_required
def api_scheduled_delete(task_id):
    """Delete a scheduled task."""
    task_store.delete_task(task_id)
    scheduler.unschedule_task(task_id)
    return jsonify({"ok": True})


@app.route("/api/scheduled/tasks/<int:task_id>/run", methods=["POST"])
@login_required
def api_scheduled_run_now(task_id):
    """Run a scheduled task immediately (manual trigger)."""
    result = scheduler.run_task_now(task_id)
    if not result.get("ok"):
        return jsonify({"ok": False, "message": result.get("message", "执行失败")}), 409
    return jsonify({"ok": True, "run": result.get("run")})


@app.route("/api/scheduled/runs", methods=["GET"])
@login_required
def api_scheduled_runs():
    """Recent run history for scheduled tasks."""
    limit = min(int(request.args.get("limit", 50)), 200)
    runs = task_store.recent_runs(limit=limit)
    return jsonify({"runs": runs})


@app.route("/api/scheduled/tasks/<int:task_id>/runs", methods=["GET"])
@login_required
def api_scheduled_task_runs(task_id):
    """Run history for one task."""
    limit = min(int(request.args.get("limit", 50)), 200)
    runs = task_store.list_runs(task_id=task_id, limit=limit)
    return jsonify({"runs": runs})


@app.route("/api/agent/traces", methods=["GET"])
@login_required
def api_agent_traces_list():
    """List agent run traces (newest first)."""
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
        success = request.args.get("success")
        if success in ("true", "false"):
            success = success == "true"
        else:
            success = None
        items = trace_store.list(
            limit=limit, offset=offset, success=success,
        )
        return jsonify({
            "traces": items,
            "count": len(items),
            "total": trace_store.count(),
        })
    except Exception as e:
        logger.exception("Failed to list traces")
        return jsonify({"error": f"Failed to list traces: {e}"}), 500


@app.route("/api/agent/traces/<trace_id>", methods=["GET"])
@login_required
def api_agent_trace_detail(trace_id: str):
    """Get a single trace with all events (replay view)."""
    trace = trace_store.get(trace_id)
    if trace is None:
        return jsonify({"error": "Trace not found"}), 404
    return jsonify(trace.to_dict(with_events=True))


@app.route("/api/agent/traces/<trace_id>", methods=["DELETE"])
@login_required
def api_agent_trace_delete(trace_id: str):
    """Delete a single trace."""
    deleted = trace_store.delete(trace_id)
    if not deleted:
        return jsonify({"error": "Trace not found"}), 404
    return jsonify({"ok": True, "deleted": trace_id})


# ---- MCP servers ----

@app.route("/api/mcp/servers", methods=["GET"])
@login_required
def api_mcp_servers():
    """List configured MCP servers and their live status."""
    mgr = getattr(agent, "mcp_manager", None)
    if mgr is None:
        return jsonify({"enabled": False, "servers": []})
    return jsonify({"enabled": True, "servers": mgr.statuses()})


@app.route("/api/mcp/tools", methods=["GET"])
@login_required
def api_mcp_tools():
    """List tools currently exposed by connected MCP servers."""
    mgr = getattr(agent, "mcp_manager", None)
    if mgr is None:
        return jsonify({"tools": []})
    return jsonify({"tools": mgr.all_tools()})


@app.route("/api/mcp/reload", methods=["POST"])
@login_required
def api_mcp_reload():
    """Re-read mcp_servers from config.yaml and (re)connect servers."""
    if request.json is None or not request.json.get("confirmed"):
        return jsonify({"error": "confirmation required"}), 400
    try:
        cfg = load_config()
        servers_cfg = cfg.get("mcp_servers") or {}
        mgr = getattr(agent, "mcp_manager", None)
        if mgr is None:
            return jsonify({"error": "MCP not available"}), 500
        from agent.mcp.manager import parse_mcp_config

        mgr.configure(parse_mcp_config(servers_cfg))
        return jsonify({"ok": True, "servers": mgr.statuses()})
    except Exception as e:
        logger.exception("MCP reload failed")
        return jsonify({"error": f"MCP reload failed: {e}"}), 500


# ---- Stats & Monitoring ----

@app.route("/health")
def health_check():
    """Health check endpoint (no auth required)."""
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/stats")
@login_required
def api_stats():
    """Return system metrics for monitoring dashboard."""
    try:
        # Database stats
        db_stats = db.get_stats()

        stats = metrics.get_stats()
        stats["conversation_count"] = db_stats["conversations"]
        stats["user_count"] = db_stats["users"]
        stats["message_count"] = db_stats["messages"]
        stats["db_size_kb"] = db_stats["db_size_kb"]
        stats["log_files"] = []
        for f in sorted(LOG_DIR.glob("*.log*"), key=lambda x: x.stat().st_size, reverse=True):
            size_kb = f.stat().st_size / 1024
            stats["log_files"].append({"name": f.name, "size_kb": round(size_kb, 1)})

        # System resources
        try:
            stats["system"] = alert_engine.system.get_resources()
        except Exception:
            stats["system"] = {"error": "Unavailable"}

        # Alert summary
        try:
            recent_alerts = alert_engine.history.get_recent(5)
            stats["alerts"] = {
                "enabled": alert_engine.get_config().get("enabled", True),
                "recent_count": len(recent_alerts),
                "recent": recent_alerts,
            }
        except Exception:
            stats["alerts"] = {"enabled": False, "recent_count": 0, "recent": []}

        return jsonify(stats)
    except Exception as e:
        logger.exception("Failed to get stats")
        return jsonify({"error": "Failed to load stats"}), 500


@app.route("/api/logs")
@login_required
def api_logs():
    """Return recent log entries from app.log."""
    try:
        log_file = LOG_DIR / "app.log"
        if not log_file.exists():
            return jsonify({"logs": []})
        lines = log_file.read_text("utf-8", errors="replace").strip().split("\n")
        # Return last 100 lines
        recent = lines[-100:]
        return jsonify({"logs": recent})
    except Exception as e:
        logger.exception("Failed to read logs")
        return jsonify({"error": "Failed to read logs"}), 500


# ---- Backup API ----

@app.route("/api/backup", methods=["GET"])
@login_required
def api_list_backups():
    """List all available backups."""
    try:
        backups = list_backups()
        return jsonify({"backups": backups})
    except Exception as e:
        logger.exception("Failed to list backups")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup", methods=["POST"])
@login_required
def api_trigger_backup():
    """Trigger a manual backup."""
    try:
        retention = request.json.get("retention", 30) if request.is_json else 30
        path = run_backup(retention=retention)
        return jsonify({
            "ok": True,
            "file": path.name,
            "size_kb": round(path.stat().st_size / 1024, 1),
        })
    except Exception as e:
        logger.exception("Backup failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/restore", methods=["POST"])
@login_required
def api_restore_backup():
    """Restore from a backup zip."""
    data = request.json
    if not data or not data.get("file"):
        return jsonify({"error": "Missing 'file' parameter"}), 400

    zip_name = data["file"]
    # Security: only allow files from the backups directory
    zip_path = BACKUP_DIR / zip_name
    if not zip_path.exists() or not zip_path.parent == BACKUP_DIR:
        return jsonify({"error": "Invalid backup file"}), 400

    dry_run = data.get("dry_run", False)
    try:
        files = restore_backup(zip_path, dry_run=dry_run)
        return jsonify({
            "ok": True,
            "dry_run": dry_run,
            "files_restored": len(files),
            "files": files,
        })
    except Exception as e:
        logger.exception("Restore failed")
        return jsonify({"error": str(e)}), 500


# ---- Alerts API ----

@app.route("/api/alerts", methods=["GET"])
@login_required
def api_alerts_status():
    """Get current alerting status (rules, resources, recent alerts)."""
    try:
        status = alert_engine.get_status()
        return jsonify(status)
    except Exception as e:
        logger.exception("Failed to get alert status")
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/check", methods=["POST"])
@login_required
def api_alerts_check():
    """Trigger a manual health check."""
    try:
        triggered = alert_engine.run_checks()
        return jsonify({
            "ok": True,
            "triggered": len(triggered),
            "alerts": triggered or [],
        })
    except Exception as e:
        logger.exception("Alert check failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/history", methods=["GET"])
@login_required
def api_alerts_history():
    """Get recent alert history."""
    try:
        limit = request.args.get("limit", 50, type=int)
        entries = alert_engine.history.get_recent(limit)
        return jsonify({"alerts": entries})
    except Exception as e:
        logger.exception("Failed to get alert history")
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/config", methods=["GET"])
@login_required
def api_alerts_config():
    """Get alert configuration."""
    try:
        cfg = alert_engine.get_config()
        # Mask webhook URL for security
        wh = cfg.get("notifications", {}).get("webhook", {})
        if wh.get("url"):
            url = wh["url"]
            wh["url_masked"] = url[:30] + "..." if len(url) > 30 else url
        # Mask email credentials
        em = cfg.get("notifications", {}).get("email", {})
        if em.get("smtp_pass"):
            em["smtp_pass"] = "***"
        return jsonify(cfg)
    except Exception as e:
        logger.exception("Failed to get alert config")
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/config", methods=["POST"])
@login_required
def api_alerts_config_update():
    """Update alert configuration."""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        alert_engine.save_config(data)
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to update alert config")
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts/test", methods=["POST"])
@login_required
def api_alerts_test():
    """Send a test notification."""
    try:
        data = request.json or {}
        channel = data.get("channel", "webhook")
        if channel == "webhook":
            from alerts import send_webhook
            cfg = alert_engine.get_config().get("notifications", {}).get("webhook", {})
            send_webhook(cfg, "Test Alert", "This is a test notification from AI Chat.", "info")
        elif channel == "email":
            from alerts import send_email
            cfg = alert_engine.get_config().get("notifications", {}).get("email", {})
            send_email(cfg, "Test Alert", "This is a test notification from AI Chat.", "info")
        else:
            return jsonify({"error": f"Unknown channel: {channel}"}), 400
        return jsonify({"ok": True, "channel": channel})
    except Exception as e:
        logger.exception("Test notification failed")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Auto-backup scheduler
# ============================================================

_backup_timer = None


def _auto_backup():
    """Run backup in background, then schedule next one."""
    global _backup_timer
    try:
        run_backup(retention=30)
        logger.info("Auto-backup completed")
    except Exception:
        logger.exception("Auto-backup failed")

    # Schedule next backup in 24 hours
    _backup_timer = threading.Timer(86400, _auto_backup)
    _backup_timer.daemon = True
    _backup_timer.start()


def _start_auto_backup():
    """Start the auto-backup scheduler after a short delay."""
    timer = threading.Timer(30, _auto_backup)  # First backup 30s after startup
    timer.daemon = True
    timer.start()


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="AI Chat Server")
    parser.add_argument("-H", "--host", default=None, help="Host to bind")
    parser.add_argument("-p", "--port", type=int, default=None, help="Port to bind")
    parser.add_argument("--workers", type=int, default=4, help="Worker threads (Waitress)")
    args = parser.parse_args()

    host = args.host or config.get("server", {}).get("host", "127.0.0.1")
    port = args.port or config.get("server", {}).get("port", 8080)

    # Graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received, stopping server...")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info("Starting AI Chat Server on %s:%d (workers=%d)", host, port, args.workers)

    # Start auto-backup scheduler
    _start_auto_backup()

    # Start scheduled-task scheduler (定时主动任务)
    try:
        _start_scheduler()
    except Exception:
        logger.exception("Failed to start scheduled-task scheduler")

    # Start alert engine (health checks every 60s)
    try:
        alert_engine.start()
    except Exception:
        logger.exception("Failed to start alert engine")

    print(f"\n{'='*40}")
    print(f"  AI Chat Server")
    print(f"  http://{host}:{port}")
    print(f"  Workers: {args.workers}")
    print(f"{'='*40}\n")

    try:
        from waitress import serve
        serve(
            app,
            host=host,
            port=port,
            threads=args.workers,
            # Must exceed the longest blocked request (approval waits up to
            # expiry_seconds=300): channel_timeout kills idle channels, and
            # a stream paused on an approval card must survive.
            channel_timeout=600,
            cleanup_interval=30,
            recv_bytes=65536,
        )
    except ImportError:
        logger.warning("Waitress not installed, falling back to Flask dev server")
        app.run(host=host, port=port, debug=False)
