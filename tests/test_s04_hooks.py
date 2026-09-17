from types import SimpleNamespace

import s04_hooks.code as chapter


def test_before_hook_can_block_without_dispatching() -> None:
    dispatched = False

    def dispatch(name, arguments):
        nonlocal dispatched
        dispatched = True
        return "executed"

    hooks = chapter.Hooks(
        before_tool_call=[lambda name, arguments: "blocked by test"],
    )
    result = chapter.execute_tool(
        "bash",
        {"command": "dir"},
        dispatch=dispatch,
        hooks=hooks,
    )

    assert result == "blocked by test"
    assert not dispatched


def test_after_hooks_run_in_order_and_receive_previous_result() -> None:
    events: list[str] = []

    def first(name, arguments, result):
        events.append(f"first:{result}")
        return result + ":one"

    def second(name, arguments, result):
        events.append(f"second:{result}")
        return result + ":two"

    result = chapter.execute_tool(
        "bash",
        {"command": "dir"},
        dispatch=lambda name, arguments: "base",
        hooks=chapter.Hooks(after_tool_call=[first, second]),
    )

    assert result == "base:one:two"
    assert events == ["first:base", "second:base:one"]


def test_permission_policy_is_registered_as_before_hook(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chapter.previous, "WORKDIR", tmp_path)
    hooks = chapter.Hooks(
        before_tool_call=[chapter.make_permission_hook(confirm=lambda *args: False)]
    )

    result = chapter.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "one"},
        dispatch=lambda name, arguments: "should not run",
        hooks=hooks,
    )

    assert result.startswith("Permission denied:")


def test_agent_loop_returns_after_hook_result_to_model() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use", id="tool-1", name="read_file", input={"path": "a.txt"}
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
    ]

    def create_message(**kwargs):
        return responses.pop(0)

    hooks = chapter.Hooks(
        after_tool_call=[lambda name, arguments, result: result + " | observed"]
    )
    messages = [{"role": "user", "content": "read a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "file content",
        system="test system",
        hooks=hooks,
    )

    assert result[-2]["content"][0]["content"] == "file content | observed"
