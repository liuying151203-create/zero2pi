from types import SimpleNamespace

import pytest

import s02_tool_use.code as chapter


def test_dispatcher_routes_file_tools_inside_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    assert chapter.dispatch_tool("write_file", {"path": "note.txt", "content": "one"}) == (
        "Wrote 3 characters to note.txt"
    )
    assert chapter.dispatch_tool("read_file", {"path": "note.txt"}) == "one"
    assert (
        chapter.dispatch_tool(
            "edit_file", {"path": "note.txt", "old_text": "one", "new_text": "two"}
        )
        == "Edited note.txt"
    )
    assert chapter.dispatch_tool("read_file", {"path": "note.txt"}) == "two"


def test_dispatcher_rejects_unknown_tool_and_path_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    assert chapter.dispatch_tool("missing", {}) == "Error: unknown tool: missing"
    result = chapter.dispatch_tool("read_file", {"path": "../outside.txt"})
    assert result.startswith("Error: Path escapes workspace")


def test_read_file_supports_a_small_line_range(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text(
        "\n".join(f"line {number}" for number in range(1, 11)),
        encoding="utf-8",
    )

    result = chapter.dispatch_tool(
        "read_file",
        {"path": "code.py", "start_line": 4, "limit": 3},
    )

    assert result.startswith("line 4\nline 5\nline 6\n\n[")
    assert "start_line=7" in result
    assert "start_line=3" in chapter.run_read("code.py", 2)


def test_agent_loop_dispatches_multiple_tools_in_order() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="tool-1", name="read_file", input={"path": "a.txt"}
                ),
                SimpleNamespace(
                    type="tool_use", id="tool-2", name="glob", input={"pattern": "*.py"}
                ),
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
    ]
    dispatched: list[tuple[str, dict]] = []

    def create_message(**kwargs):
        return responses.pop(0)

    def dispatch(name, arguments):
        dispatched.append((name, arguments))
        return f"result for {name}"

    messages = [{"role": "user", "content": "inspect files"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=dispatch,
        system="test system",
    )

    assert dispatched == [
        ("read_file", {"path": "a.txt"}),
        ("glob", {"pattern": "*.py"}),
    ]
    assert result[-2]["content"][0]["content"] == "result for read_file"
    assert result[-2]["content"][1]["content"] == "result for glob"


@pytest.mark.parametrize("limit", [None, 10000])
def test_read_budget_caps_lines_and_reports_exact_continuation(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("\n".join(f"line {i}" for i in range(2300)), encoding="utf-8")
    output = chapter.run_read("code.py", limit=limit)
    body = output.split("\n\n[", 1)[0]
    assert len(body.splitlines()) == 2000
    assert "start_line=2001" in output


def test_read_budget_counts_utf8_bytes_and_does_not_skip_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    lines = [f"{i}:" + "中" * 1000 for i in range(25)]
    (tmp_path / "code.py").write_text("\n".join(lines), encoding="utf-8")
    output = chapter.run_read("code.py")
    body = output.split("\n\n[", 1)[0]
    shown = len(body.splitlines())
    assert len(body.encode("utf-8")) <= chapter.TOOL_MAX_BYTES
    assert body.splitlines() == lines[:shown]
    assert f"start_line={shown + 1}" in output
    assert chapter.run_read("code.py", start_line=shown + 1) == "\n".join(lines[shown:])


def test_read_reports_oversized_line_without_skipping_it(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("中" * 20000 + "\nnext", encoding="utf-8")
    output = chapter.run_read("code.py")
    assert output.startswith("Error:")
    assert "next" not in output


def test_grep_locates_utf8_code_with_line_numbers_and_literal_matching(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("# 中文注释\na.b = 1\naxb = 2", encoding="utf-8")
    output = chapter.dispatch_tool("grep", {"path": "code.py", "pattern": "a.b"})
    assert ":2: a.b = 1" in output
    assert "axb" not in output
    assert ":1: # 中文注释" in chapter.run_grep("code.py", "中文")
    assert chapter.run_grep("code.py", "不存在") == "(no matches)"
    assert chapter.run_grep("../outside.py", "a").startswith("Error: Path escapes workspace")


def test_grep_bounds_matches_and_long_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text(
        "\n".join("match" + "x" * 1000 for _ in range(105)), encoding="utf-8"
    )
    output = chapter.run_grep("code.py", "match", limit=10000)
    assert 0 < output.count("匹配行已截断") <= 100
    assert "匹配结果已达上限" in output
    assert len(output.split("\n[匹配结果", 1)[0].encode("utf-8")) <= chapter.TOOL_MAX_BYTES
    (tmp_path / "code.py").write_text("match\n" * 105, encoding="utf-8")
    assert chapter.run_grep("code.py", "match", limit=10000).count(": match") == 100


@pytest.mark.parametrize("returncode", [0, 2])
def test_shell_output_retains_tail_and_archives_full_text(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    full = "中文日志\n" * 10000 + "最后的失败证据"
    monkeypatch.setattr(
        chapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=full, stderr="", returncode=returncode),
    )
    output = chapter.run_bash("offline-command")
    assert "最后的失败证据" in output
    body = output.split("\n\n[", 1)[0]
    if returncode:
        assert body.startswith("Error: command exited with code 2")
        body = body.split("\n", 1)[1]
    assert len(body.encode("utf-8")) <= chapter.TOOL_MAX_BYTES
    archive = next((tmp_path / ".sessions" / "tool-results").glob("*.txt"))
    assert archive.read_text(encoding="utf-8") == full
    assert archive.as_posix() in output


def test_shell_nonzero_exit_is_reported_as_an_error(monkeypatch):
    monkeypatch.setattr(
        chapter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="invalid option", returncode=2),
    )
    assert chapter.run_bash("offline-command").startswith("Error: command exited with code 2")
