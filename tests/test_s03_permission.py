from types import SimpleNamespace

import s03_permission.code as chapter


def test_read_only_tools_are_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    assert chapter.check_permission("read_file", {"path": "README.md"}).status is (
        chapter.PermissionStatus.ALLOW
    )
    assert chapter.check_permission("glob", {"pattern": "*.py"}).status is (
        chapter.PermissionStatus.ALLOW
    )


def test_ranged_read_stays_read_only_and_returns_requested_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)
    (tmp_path / "code.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    arguments = {"path": "code.py", "start_line": 2, "limit": 2}

    decision = chapter.check_permission("read_file", arguments)
    result = chapter.dispatch_tool("read_file", arguments)

    assert decision.status is chapter.PermissionStatus.ALLOW
    assert result == "two\nthree\n... (1 more lines)"


def test_writes_need_confirmation_and_path_escape_is_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter, "WORKDIR", tmp_path)

    decision = chapter.check_permission("write_file", {"path": "note.txt", "content": "one"})
    assert decision.status is chapter.PermissionStatus.ASK

    denied = chapter.check_permission(
        "edit_file", {"path": "../outside.txt", "old_text": "a", "new_text": "b"}
    )
    assert denied.status is chapter.PermissionStatus.DENY


def test_bash_rules_distinguish_allow_ask_and_deny() -> None:
    assert chapter.check_permission("bash", {"command": "dir"}).status is (
        chapter.PermissionStatus.ALLOW
    )
    assert chapter.check_permission("bash", {"command": "del note.txt"}).status is (
        chapter.PermissionStatus.ASK
    )
    assert chapter.check_permission("bash", {"command": "format C:"}).status is (
        chapter.PermissionStatus.DENY
    )


def test_execute_tool_returns_denial_without_dispatching() -> None:
    dispatched = False

    def dispatch(name, arguments):
        nonlocal dispatched
        dispatched = True
        return "executed"

    result = chapter.execute_tool(
        "bash",
        {"command": "format C:"},
        dispatch=dispatch,
    )

    assert result.startswith("Permission denied:")
    assert not dispatched


def test_agent_loop_returns_permission_result_to_model() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="tool-1", name="write_file", input={"path": "a.txt"}
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="permission handled")]),
    ]

    def create_message(**kwargs):
        return responses.pop(0)

    messages = [{"role": "user", "content": "write a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "should not run",
        system="test system",
        confirm=lambda name, arguments, reason: False,
    )

    assert result[-2]["content"][0]["content"].startswith("Permission denied:")
    assert result[-1]["content"][0].text == "permission handled"
