from pathlib import Path
from types import SimpleNamespace

import pytest

import s09_runtime_streaming.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


class FakeStream:
    def __init__(self, chunks: list[str], final_message: object, error: Exception | None = None):
        self.text_stream = self._text_stream(chunks, error)
        self.final_message = final_message

    @staticmethod
    def _text_stream(chunks: list[str], error: Exception | None):
        yield from chunks
        if error is not None:
            raise error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def get_final_message(self):
        return self.final_message


def test_default_session_root_is_scoped_to_s09() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s09")


def test_streaming_requester_emits_deltas_and_returns_final_message() -> None:
    final_message = SimpleNamespace(content=[SimpleNamespace(type="text", text="你好")])
    requests: list[dict[str, object]] = []
    events: list[chapter.AgentEvent] = []

    class Messages:
        def stream(self, **kwargs):
            requests.append(kwargs)
            return FakeStream(["你", "好"], final_message)

    requester = chapter.StreamingModelRequester(
        client=SimpleNamespace(messages=Messages()),
        model="test-model",
        timeout_seconds=12,
        emit=events.append,
    )

    response = requester(messages=[], system="test", tools=[], max_tokens=100)

    assert response is final_message
    assert [event.type for event in events] == [
        "model_request",
        "assistant_delta",
        "assistant_delta",
    ]
    assert [event.data["text"] for event in events[1:]] == ["你", "好"]
    assert requests[0]["model"] == "test-model"


def test_blocking_requester_does_not_emit_assistant_delta() -> None:
    events: list[chapter.AgentEvent] = []

    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="内部摘要")])

    requester = chapter.BlockingModelRequester(
        client=SimpleNamespace(messages=Messages()),
        model="test-model",
        timeout_seconds=12,
        emit=events.append,
    )

    requester(messages=[], system="summary", tools=[], max_tokens=100)

    assert [event.type for event in events] == ["model_request"]


def test_terminal_sink_prints_streamed_answer_only_once(capsys) -> None:
    terminal = chapter.TerminalEventSink()

    terminal(chapter.AgentEvent(type="assistant_delta", data={"text": "你"}))
    terminal(chapter.AgentEvent(type="assistant_delta", data={"text": "好"}))
    terminal(
        chapter.AgentEvent(
            type="assistant_message",
            data={"message": _message("assistant", [{"type": "text", "text": "你好"}])},
        )
    )

    output = capsys.readouterr().out
    assert output == "Agent  你好\n"


def test_terminal_sink_falls_back_to_complete_message_without_deltas(capsys) -> None:
    terminal = chapter.TerminalEventSink()

    terminal(
        chapter.AgentEvent(
            type="assistant_message",
            data={"message": _message("assistant", [{"type": "text", "text": "完整回答"}])},
        )
    )

    assert capsys.readouterr().out == "Agent  完整回答\n"


def test_stream_error_ends_partial_terminal_line(capsys) -> None:
    terminal = chapter.TerminalEventSink()
    final_message = SimpleNamespace(content=[])

    class Messages:
        def stream(self, **kwargs):
            return FakeStream(["部分回答"], final_message, RuntimeError("连接中断"))

    requester = chapter.StreamingModelRequester(
        client=SimpleNamespace(messages=Messages()),
        model="test-model",
        timeout_seconds=12,
        emit=terminal,
    )

    with pytest.raises(RuntimeError, match="模型请求失败：连接中断"):
        requester(messages=[], system="test", tools=[], max_tokens=100)

    assert capsys.readouterr().out.endswith("部分回答\n")


def test_agent_loop_persists_only_complete_messages() -> None:
    final_messages = [
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
    saved: list[chapter.Message] = []

    class Messages:
        def stream(self, **kwargs):
            final_message = final_messages.pop(0)
            text = "" if final_message.content[0].type == "tool_use" else "读取完成"
            return FakeStream([text], final_message)

    requester = chapter.StreamingModelRequester(
        client=SimpleNamespace(messages=Messages()),
        model="test-model",
        timeout_seconds=12,
        emit=events.append,
    )

    result = chapter.agent_loop(
        [_message("user", "读取项目说明")],
        create_message=requester,
        dispatch=lambda name, arguments: "文件内容",
        system="test system",
        hooks=chapter.Hooks(),
        emit=events.append,
        save_message=saved.append,
    )

    assert [message["role"] for message in saved] == ["assistant", "user", "assistant"]
    assert result[-1]["content"][0]["text"] == "读取完成"
    assert [event.type for event in events] == [
        "model_request",
        "assistant_message",
        "tool_call",
        "tool_result",
        "model_request",
        "assistant_delta",
        "assistant_message",
        "agent_end",
    ]
