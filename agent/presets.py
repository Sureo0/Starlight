"""
Agent Presets - Pre-configured agent setups for common use cases.

Provides three levels of factory functions:
  - create_agent()        — one-liner, auto-detects everything
  - create_default_agent() — full-featured with all tools
  - create_minimal_agent() — lightweight, memory-only

Plus preset configurations for common scenarios.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.chat_completion import ChatCompletionTool
from agent.tools.memory_tools import (
    MemoryQueryTool,
    MemoryStoreTool,
    MemoryForgetTool,
    MemoryListTool,
)
from agent.tools.web_search import WebSearchTool
from agent.tools.file_tools import ReadFileTool, ReadFilesTool, WriteFileTool, ListFilesTool
from agent.tools.code_executor import CodeExecutorTool
from agent.tools.weather import WeatherTool

try:
    from agent.tools.delegate import SubagentTool
    _HAS_SUBAGENT = True
except ImportError:  # pragma: no cover
    SubagentTool = None
    _HAS_SUBAGENT = False

try:
    from agent.approval import ApprovalManager, ApprovalStore
    _HAS_APPROVAL = True
except ImportError:  # pragma: no cover
    ApprovalManager = ApprovalStore = None
    _HAS_APPROVAL = False

from agent.security.sandbox import SandboxConfig
from agent.security.file_guard import FileGuardConfig
from agent.security.permissions import ToolPermission, PermissionLevel, ToolCategory
from agent.security.rate_limiter import RateLimiter
from agent.memory.service import MemoryService

try:
    from agent.mcp.manager import MCPManager as _MCPManager
    from agent.tools.mcp_tool import MCPTool
    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MCPManager = None
    MCPTool = None
    _MCP_AVAILABLE = False

try:
    from agent.memory.extractor import MemoryExtractor
    _HAS_EXTRACTOR = True
except ImportError:  # pragma: no cover
    MemoryExtractor = None
    _HAS_EXTRACTOR = False

try:
    from agent.planning.planner import PlanGenerator, StepReviewer
    _HAS_PLANNER = True
except ImportError:  # pragma: no cover
    PlanGenerator = None
    _HAS_PLANNER = False

logger = logging.getLogger("agent.presets")


# ============================================================
# One-liner factory (auto-detects everything)
# ============================================================

def create_agent(
    llm_client,
    db=None,
    workspace_dir: str | Path = ".",
    username: str | None = None,
    user_id: int | None = None,
    tavily_api_key: str | None = None,
    preset: str = "default",
    **kwargs,
) -> AgentOrchestrator:
    """
    Create a fully configured agent in one call.

    Args:
        llm_client: AgentLLMClient instance.
        db: Database proxy (enables memory tools).
        workspace_dir: Root directory for file tools.
        username: Current user (for permission checks).
        user_id: Database user id (for memory scoping). Falls back to
                 resolving username via db.get_user().
        tavily_api_key: Tavily API key (enables web search).
        preset: Preset name — "default", "minimal", "coder", "analyst".
        **kwargs: Override any AgentConfig field (e.g. temperature=0.3).

    Returns:
        Ready-to-use AgentOrchestrator.

    Examples:
        # Simplest usage
        agent = create_agent(llm_client)

        # With all options
        agent = create_agent(
            llm_client,
            db=db,
            workspace_dir="/project",
            username="alice",
            tavily_api_key="tvly-xxx",
            preset="coder",
            temperature=0.2,
        )
    """
    preset_config = PRESETS.get(preset, PRESETS["default"])

    # Resolve user_id from db if not given
    if user_id is None and db is not None and username:
        try:
            user = db.get_user(username)
            user_id = user["id"] if user else None
        except Exception:
            user_id = None

    # Extract mount wiring from kwargs (not AgentConfig fields)
    mount_manager = kwargs.pop("mount_manager", None)
    mount_approval_cb = kwargs.pop("mount_approval_cb", None)

    return create_default_agent(
        llm_client=llm_client,
        db=db,
        workspace_dir=str(workspace_dir),
        tavily_api_key=tavily_api_key,
        system_prompt=preset_config.system_prompt,
        config_overrides={**preset_config.config_overrides, **kwargs},
        username=username,
        user_id=user_id,
        security=preset_config.security,
        mount_manager=mount_manager,
        mount_approval_cb=mount_approval_cb,
    )


# ============================================================
# Full-featured factory
# ============================================================

def create_default_agent(
    llm_client,
    db=None,
    workspace_dir: str = ".",
    tavily_api_key: str | None = None,
    system_prompt: str | None = None,
    config_overrides: dict | None = None,
    username: str | None = None,
    user_id: int | None = None,
    security: bool = True,
    trace_sink=None,
    mount_manager=None,
    mount_approval_cb=None,
) -> AgentOrchestrator:
    """
    Create an AgentOrchestrator with all tools and security enabled.

    This is the main entry point for setting up the agent with full features.

    Args:
        trace_sink: Callable(trace) invoked when a run finishes. Passed to
            sub-agent (delegate) children so their traces get persisted too.
        mount_manager: Optional MountManager. When set, file tools accept
            "mount:<id>/rel/path" references into mounted folders.
        mount_approval_cb: Optional callable(tool_name, args) -> ToolResult
            dict | None. Called before accessing a mounted folder; return
            None to allow, or a result dict to block.
    """
    config = AgentConfig(
        system_prompt=system_prompt or _default_system_prompt(),
        **(config_overrides or {}),
    )

    # Build tool registry with all tools
    tools = ToolRegistry()

    # Chat completion (multi-step reasoning)
    tools.register(ChatCompletionTool(llm_client))

    # Long-term memory (if DB available)
    memory_service = None
    memory_extractor = None
    if db:
        memory_service = MemoryService(db, user_id=user_id)
        tools.register(MemoryQueryTool(memory_service=memory_service))
        tools.register(MemoryStoreTool(memory_service=memory_service))
        tools.register(MemoryForgetTool(memory_service=memory_service))
        tools.register(MemoryListTool(memory_service=memory_service))
        if _HAS_EXTRACTOR:
            memory_extractor = MemoryExtractor(
                llm=llm_client,
                memory_service=memory_service,
                min_turn_messages=2,
                min_user_chars=8,
                quality_gate=bool(config.memory_quality_gate),
            )

    # File tools (with FileGuard security). When a mount_manager and an
    # approval callback are provided (wired by the app), paths of the form
    # "mount:<id>/rel/path" resolve into mounted folders and every access
    # goes through the human-in-the-loop approval callback.
    guard_config = FileGuardConfig(allowed_dirs=[workspace_dir])
    tools.register(ReadFileTool(workspace_dir, guard_config=guard_config,
                                mount_manager=mount_manager, approval_cb=mount_approval_cb))
    tools.register(ReadFilesTool(workspace_dir, guard_config=guard_config,
                                 mount_manager=mount_manager, approval_cb=mount_approval_cb))
    tools.register(WriteFileTool(workspace_dir, guard_config=guard_config,
                                 mount_manager=mount_manager, approval_cb=mount_approval_cb))
    tools.register(ListFilesTool(workspace_dir, guard_config=guard_config,
                                 mount_manager=mount_manager, approval_cb=mount_approval_cb))

    # Code executor (with sandbox) — sandbox cwd = the agent's workspace so
    # executed code can read files the agent wrote with file tools.
    sandbox_config = SandboxConfig(timeout=30)
    tools.register(CodeExecutorTool(sandbox_config=sandbox_config, work_dir=workspace_dir))

    # Web search
    tools.register(WebSearchTool(tavily_api_key=tavily_api_key))

    # Weather tool
    tools.register(WeatherTool())

    # Security: permissions
    permissions = ToolPermission()
    if username:
        permissions.register_user(username, PermissionLevel.USER)

    # Security: rate limiter
    rate_limiter = RateLimiter()

    # MCP servers: tools from external MCP servers are registered here so
    # the rest of the agent treats them like built-ins. The manager's
    # tool_sink re-registers tools when a server (re)connects.
    mcp_manager = None
    mcp_tool_names: set[str] = set()
    if config.mcp_enabled and _MCP_AVAILABLE and MCPTool is not None:
        try:
            from agent.mcp.manager import MCPManager, parse_mcp_config

            mcp_manager = MCPManager()

            def _sync_mcp_tools():
                """(Re)register MCP tools into the registry + permissions."""
                nonlocal mcp_tool_names
                if mcp_manager is None:
                    return
                # Remove tools from servers that disconnected
                alive = {
                    f"{t['server']}__{t['name']}" for t in mcp_manager.all_tools()
                }
                for name in list(mcp_tool_names - alive):
                    tools.unregister(name)
                    mcp_tool_names.discard(name)
                # Register new tools
                for t in mcp_manager.all_tools():
                    fq = f"{t['server']}__{t['name']}"
                    if fq in mcp_tool_names:
                        continue
                    tools.register(MCPTool(
                        manager=mcp_manager,
                        server_name=t["server"],
                        tool_name=t["name"],
                        description=t["description"],
                        input_schema=t["inputSchema"],
                        permission=t["permission"],
                    ))
                    mcp_tool_names.add(fq)
                    # Permission wiring: every MCP tool needs a category so
                    # the permission system can decide who may call it.
                    #   user      -> USER default categories (read/write/execute/...)
                    #   admin     -> READ (admin users get everything anyway)
                    #   read-only -> READ only
                    if t["permission"] == "read-only":
                        permissions.add_category_override(
                            fq, ToolCategory.READ
                        )
                    else:
                        # Default: treat as a user-level tool (READ+WRITE+EXECUTE
                        # are all in USER defaults, so USERs can call it).
                        permissions.add_category_override(
                            fq, ToolCategory.EXECUTE
                        )
                logger.info("MCP tools synced: %d", len(mcp_tool_names))

            mcp_manager._tool_sink = _sync_mcp_tools
            mcp_manager.configure(parse_mcp_config(config.mcp_servers or {}))
            # Initial sync for servers already connected
            _sync_mcp_tools()
            agent_mcp = mcp_manager  # keep reference for teardown
        except Exception:
            logger.exception("MCP setup failed; continuing without MCP tools")
            mcp_manager = None

    # Create orchestrator
    agent = AgentOrchestrator(
        llm=llm_client,
        tools=tools,
        config=config,
        db=db,
        username=username,
    )
    if mcp_manager is not None:
        agent.mcp_manager = mcp_manager

    # Human-in-the-loop approval: if a db is available, wire the approval
    # manager so side-effectful tools CAN pause for the user's confirmation.
    # Whether they actually pause is controlled by config.approval_enabled
    # (the app enables it explicitly; library users opt in).
    if _HAS_APPROVAL and db is not None:
        try:
            from agent.approval import ApprovalStore, ApprovalManager
            agent.approval_manager = ApprovalManager(
                store=ApprovalStore(db),
                expiry_seconds=int(config.approval_expiry),
            )
        except Exception:
            logger.exception("ApprovalManager setup failed; tools run unapproved")

    # Sub-agent (delegate) tool: lets the agent hand a focused sub-task to a
    # nested agent. Registered AFTER the orchestrator exists (it needs a
    # reference to the parent orchestrator to spawn children).
    if _HAS_SUBAGENT and config.tools_enabled:
        try:
            tools.register(SubagentTool(
                llm=llm_client,
                parent=agent,
                workspace_dir=workspace_dir,
                username=username,
                trace_sink=trace_sink,
                max_timeout=float(config.subagent_max_duration),
            ))
        except Exception:
            logger.exception("SubagentTool registration failed; continuing without it")

    # Wire up long-term memory
    if memory_service is not None:
        agent.memory_service = memory_service
        agent.memory_extractor = memory_extractor

    # Wire up planning (Plan-then-Execute)
    if _HAS_PLANNER and config.planning_enabled:
        agent.plan_generator = PlanGenerator(
            llm=llm_client,
            min_user_chars=config.plan_min_user_chars,
        )
        # LLM step reviewer: re-checks plan progress periodically so
        # heuristic tool-name matching can be corrected (fail-soft).
        if config.plan_review_enabled:
            agent.plan_reviewer = StepReviewer(
                llm=llm_client,
                max_review_calls=config.plan_review_max_calls,
            )

    # Wire up security components
    if security:
        agent._permissions = permissions
        agent._rate_limiter = rate_limiter
    else:
        config.security_enabled = False

    return agent


# ============================================================
# Minimal factory
# ============================================================

def create_minimal_agent(
    llm_client,
    db=None,
    system_prompt: str | None = None,
    username: str | None = None,
    user_id: int | None = None,
) -> AgentOrchestrator:
    """
    Create a lightweight agent with only memory tools.
    No file access, no web search, no code execution.
    """
    config = AgentConfig(
        system_prompt=system_prompt or _default_system_prompt(),
        tools_enabled=True,
    )

    tools = ToolRegistry()
    if db:
        memory_service = MemoryService(db, user_id=user_id)
        tools.register(MemoryQueryTool(memory_service=memory_service))
        tools.register(MemoryStoreTool(memory_service=memory_service))

        memory_extractor = MemoryExtractor(
            llm=llm_client, memory_service=memory_service
        ) if _HAS_EXTRACTOR else None
    else:
        memory_service, memory_extractor = None, None

    agent = AgentOrchestrator(
        llm=llm_client,
        tools=tools,
        config=config,
        db=db,
        username=username,
    )
    if memory_service is not None:
        agent.memory_service = memory_service
        agent.memory_extractor = memory_extractor

    return agent


# ============================================================
# System prompts
# ============================================================

def _default_system_prompt() -> str:
    return """你是一个智能 AI 助手，具备以下能力：

## 核心能力
- 使用工具来完成复杂任务（搜索网络、读写文件、执行代码等）
- 多步推理：可以先收集信息，再基于信息做出判断
- 记忆管理：可以查询和存储对话历史

## 工具使用原则
- 需要最新信息时，优先使用网络搜索或天气工具
- 需要处理数据或计算时，使用代码执行工具
- 需要读取本地文件时，使用文件读取工具
- 每次工具调用后，仔细分析结果再决定下一步

## 长期记忆（重要）
- 你拥有跨会话的长期记忆能力
- 对话开始时，相关的历史记忆会自动注入到你的上下文中，直接引用即可
- 当用户问『我之前说过…』『你记得…』『我上次…』时，用 memory_query 搜索记忆
- 用户明确表达偏好、事实、进行中任务时，用 memory_store 主动记住
- 用户要求忘记某条信息时，用 memory_forget 删除
- 不要编造记忆内容，检索不到就如实说明

## 多步规划（重要）
- 复杂任务开始前，系统会为你生成一份执行计划并注入上下文
- 每轮工具调用后，计划进度会更新，请严格按照计划推进，不要跳过步骤
- 计划中的步骤完成后自动标记；如果计划已过时或不合理，可以自行调整顺序

## 工具调用效率（重要）
- 一次可以同时调用多个工具，不要一个一个串行调用
- 已经获取过的信息不要重复调用工具获取
- 如果已经读取了文件A的内容，不需要再读取一次
- 搜索结果已经返回后，直接基于结果回答，不要再次搜索同样的关键词
- 优先用 list_files 了解目录结构，再有针对性地读取需要的文件
- 需要读取多个文件时，用 read_files 批量读取（一次最多15个），不要逐个 read_file
- 写入文件时，一次性写入完整内容，不要分多次写入同一个文件

## 回复规范
- 用中文回复，除非用户使用其他语言
- 回答要准确、简洁、有帮助
- 如果不确定，如实告知而不是编造信息
- 需要时使用 Markdown 格式增强可读性

当前日期时间：{current_date_time}"""


def _coder_system_prompt() -> str:
    return """你是一个专业的编程助手，专注于代码开发和调试。

## 核心能力
- 编写、调试、重构代码
- 阅读和分析代码文件
- 执行代码并验证结果
- 搜索技术文档和解决方案

## 工具使用原则
- 先读取相关文件理解上下文，再进行修改
- 修改后运行代码验证是否正确
- 遇到问题时搜索相关文档
- 生成代码时考虑错误处理和边界情况

## 编码规范
- 遵循语言的最佳实践
- 添加必要的注释
- 使用有意义的变量名
- 保持代码简洁可读

## 回复规范
- 代码用 Markdown 代码块包裹
- 解释关键设计决策
- 如有多个方案，说明各自的优劣

当前日期时间：{current_date_time}"""


def _analyst_system_prompt() -> str:
    return """你是一个数据分析助手，专注于数据处理和洞察发现。

## 核心能力
- 分析数据并提取关键洞察
- 使用代码处理数据（Python/pandas/numpy）
- 生成数据可视化
- 搜索补充数据和背景信息

## 工具使用原则
- 先读取数据文件了解结构
- 使用代码执行工具进行数据处理
- 生成图表辅助分析
- 搜索补充行业背景知识

## 分析规范
- 基于数据说话，避免主观臆断
- 指出数据的局限性
- 提供可操作的建议
- 用图表和表格辅助说明

## 回复规范
- 结构化呈现分析结果
- 关键数字加粗标注
- 提供数据来源和方法说明

当前日期时间：{current_date_time}"""


def _creative_system_prompt() -> str:
    return """你是一个富有创意的助手，擅长创意写作和头脑风暴。

## 核心能力
- 创意写作（文章、故事、文案）
- 头脑风暴和创意生成
- 内容润色和优化
- 搜索灵感和参考资料

## 工具使用原则
- 写作前搜索相关素材和灵感
- 使用文件工具保存草稿
- 多次迭代优化内容

## 创作规范
- 保持原创性和独特性
- 适应不同的写作风格和语气
- 注重读者体验

## 回复规范
- 根据场景调整语气
- 提供多个创意选项
- 接受反馈并快速迭代

当前日期时间：{current_date_time}"""


# ============================================================
# Preset configurations (must come after prompt functions)
# ============================================================

class AgentPreset:
    """A named preset configuration for the agent."""

    def __init__(
        self,
        system_prompt: str,
        config_overrides: dict | None = None,
        security: bool = True,
    ):
        self.system_prompt = system_prompt
        self.config_overrides = config_overrides or {}
        self.security = security


PRESETS: dict[str, AgentPreset] = {
    "default": AgentPreset(
        system_prompt=_default_system_prompt(),
        config_overrides={
            "temperature": 0.7,
            "context_window": 32000,
        },
    ),
    "minimal": AgentPreset(
        system_prompt=_default_system_prompt(),
        config_overrides={
            "temperature": 0.7,
            "tools_enabled": True,
        },
        security=False,
    ),
    "coder": AgentPreset(
        system_prompt=_coder_system_prompt(),
        config_overrides={
            "temperature": 0.2,
            "context_window": 16000,
        },
    ),
    "analyst": AgentPreset(
        system_prompt=_analyst_system_prompt(),
        config_overrides={
            "temperature": 0.5,
            "context_window": 12000,
        },
    ),
    "creative": AgentPreset(
        system_prompt=_creative_system_prompt(),
        config_overrides={
            "temperature": 0.9,
            "context_window": 12000,
        },
    ),
}
