"""
Tests for the tool-call budget and context window settings.
"""

from agent.orchestrator import AgentConfig


def test_default_tool_call_budget():
    """max_tool_calls default is 300 to support file-heavy tasks."""
    assert AgentConfig().max_tool_calls == 300


def test_default_context_window():
    """context_window default is 32000 (was 12000) to reduce re-reads."""
    assert AgentConfig().context_window == 32000


def test_config_values_reach_agent():
    """app.py wires config.yaml agent settings into the agent."""
    import app as app_module
    agent = app_module.agent
    assert agent.config.max_tool_calls == 300
    assert agent.config.context_window == 32000
    assert agent.config.execution_timeout == 600
