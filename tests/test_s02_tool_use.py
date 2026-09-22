from types import SimpleNamespace

import s02_tool_use.code as chapter


def test_dispatcher_routes_file_tools_inside_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    assert chapter.dispatch_tool("write_file", {"path": "note.txt", "content": "one"}) == (
        "Wrote 3 characters to note.txt"
    )
    assert chapter.dispatch_tool("read_file", {"path": "note.txt"}) == "one"
    assert chapter.dispatch_tool(
        "edit_file", {"path": "note.txt", "old_text": "one", "new_text": "two"}
    ) == "Edited note.txt"
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

    assert result == "line 4\nline 5\nline 6\n... (4 more lines)"
    assert chapter.run_read("code.py", 2) == "line 1\nline 2\n... (8 more lines)"


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
