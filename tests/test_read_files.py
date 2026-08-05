"""
Tests for the batch file read tool (read_files) — the fix for slow
file-heavy tasks that previously burned tool-call budget one file per call.
"""

from agent.tools.file_tools import ReadFilesTool


def test_read_files_batch(tmp_path):
    """Reading multiple files in one call returns all contents."""
    (tmp_path / "a.py").write_text("print('a')", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('b')", encoding="utf-8")
    (tmp_path / "c.md").write_text("# Title", encoding="utf-8")

    tool = ReadFilesTool(workspace_dir=str(tmp_path))
    result = tool.execute(paths=["a.py", "b.py", "c.md"])

    assert result.success
    assert result.output["read_count"] == 3
    contents = {f["path"]: f["content"] for f in result.output["files"]}
    assert contents["a.py"] == "print('a')"
    assert contents["b.py"] == "print('b')"
    assert contents["c.md"] == "# Title"


def test_read_files_missing_path_reports_error(tmp_path):
    (tmp_path / "ok.py").write_text("x", encoding="utf-8")
    tool = ReadFilesTool(workspace_dir=str(tmp_path))

    result = tool.execute(paths=["ok.py", "missing.py"])

    assert result.success  # batch still succeeds overall
    assert result.output["read_count"] == 2
    by_path = {f["path"]: f for f in result.output["files"]}
    assert "content" in by_path["ok.py"]
    assert "error" in by_path["missing.py"]


def test_read_files_limit_15(tmp_path):
    tool = ReadFilesTool(workspace_dir=str(tmp_path))
    result = tool.execute(paths=[f"f{i}.py" for i in range(20)])
    assert not result.success
    assert "15" in result.error


def test_read_files_requires_list(tmp_path):
    tool = ReadFilesTool(workspace_dir=str(tmp_path))
    assert not tool.execute(paths=None).success
    assert not tool.execute(paths="a.py").success


def test_read_files_registered_in_full_agent(tmp_db, admin_user):
    """The full agent exposes read_files alongside read_file."""
    from agent.presets import create_agent
    from conftest import ScriptedLLM

    agent = create_agent(
        llm_client=ScriptedLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )
    names = [t.name for t in agent.tools.all_tools()]
    assert "read_files" in names
    assert "read_file" in names
