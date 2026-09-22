from pathlib import Path
from types import SimpleNamespace

import s08_runtime_events.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


def test_default_session_root_is_scoped_to_s08() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s08")


def test_agent_loop_emits_tool_lifecycle_without_printing(capsys) -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    name="read_file",
                    input={"path": "README.md"},
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="读取完成")]),
    ]
    events: list[chapter.AgentEvent] = []

    result = chapter.agent_loop(
        [_message("user", "读取项目说明")],
        create_message=lambda **kwargs: responses.pop(0),
        dispatch=lambda name, arguments: "文件内容" * 100,
        system="test system",
        hooks=chapter.Hooks(),
        emit=events.append,
    )

    assert [event.type for event in events] == [
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "agent_end",
    ]
    assert events[2].data["output"] == "文件内容" * 100
    assert result[-1]["content"][0]["text"] == "读取完成"
    assert capsys.readouterr().out == ""


def test_model_requester_emits_before_calling_client() -> None:
    requests: list[dict[str, object]] = []
    events: list[chapter.AgentEvent] = []

    class Messages:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(content=[])

    requester = chapter.EventModelRequester(
        client=SimpleNamespace(messages=Messages()),
        model="test-model",
        timeout_seconds=12,
        emit=events.append,
    )

    requester(messages=[], system="test", tools=[], max_tokens=100)

    assert events == [
        chapter.AgentEvent(type="model_request", data={"timeout_seconds": 12})
    ]
    assert requests[0]["model"] == "test-model"


def test_compacted_requester_reports_outcome_and_replaces_context() -> None:
    events: list[chapter.AgentEvent] = []
    requested_messages: list[list[chapter.Message]] = []
    active_context = [_message("user", "压缩后的上下文")]
    outcome = chapter.CompactionOutcome(
        summary_kind="model",
        before_chars=1800,
        after_chars=700,
        summarized_messages=4,
        retained_messages=2,
    )
    session = SimpleNamespace(build_context=lambda: active_context)
    compactor = SimpleNamespace(compact_if_needed=lambda: outcome)

    requester = chapter.CompactedContextRequester(
        create_message=lambda **kwargs: requested_messages.append(kwargs["messages"]),
        session=session,
        compactor=compactor,
        emit=events.append,
    )
    requester(messages=[_message("user", "旧的内存上下文")])

    assert requested_messages == [active_context]
    assert events[0].type == "compaction"
    assert events[0].data["before_chars"] == 1800
    assert events[0].data["after_chars"] == 700


def test_print_event_renders_existing_terminal_styles(capsys) -> None:
    chapter.print_event(
        chapter.AgentEvent(
            type="tool_call",
            data={"name": "read_file", "arguments": {"path": "README.md"}},
        )
    )
    chapter.print_event(
        chapter.AgentEvent(
            type="assistant_message",
            data={"message": _message("assistant", [{"type": "text", "text": "完成"}])},
        )
    )

    output = capsys.readouterr().out
    assert "工具" in output
    assert "read_file" in output
    assert "Agent" in output
    assert "完成" in output
