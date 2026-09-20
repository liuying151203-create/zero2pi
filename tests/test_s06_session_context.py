from pathlib import Path
from types import SimpleNamespace

import s06_session_context.code as chapter


def test_default_session_root_is_scoped_to_s06() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s06")


def test_store_round_trips_message_entries(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message({"role": "user", "content": "继续实现会话"})
    session.append_message(
        {"role": "assistant", "content": [{"type": "text", "text": "正在处理"}]}
    )

    entries = session.load_entries()

    assert [type(entry) for entry in entries] == [chapter.MessageEntry, chapter.MessageEntry]
    assert [entry.message["role"] for entry in entries] == ["user", "assistant"]


def test_build_context_creates_a_separate_message_list() -> None:
    entries = [
        chapter.MessageEntry.from_message({"role": "user", "content": "第一条"}),
        chapter.MessageEntry.from_message({"role": "assistant", "content": "第二条"}),
    ]

    context = chapter.build_session_context(entries)

    assert context == [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "第二条"},
    ]
    assert context is not entries


def test_context_wrapper_rebuilds_messages_before_each_model_request(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message({"role": "user", "content": "读取文件"})
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    name="read_file",
                    input={"path": "a.txt"},
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="完成")]),
    ]
    requested_contexts: list[list[chapter.Message]] = []

    def create_message(**kwargs):
        requested_contexts.append(kwargs["messages"])
        return responses.pop(0)

    result = chapter.agent_loop(
        session.build_context(),
        create_message=chapter.with_session_context(create_message, session.build_context),
        dispatch=lambda name, arguments: "文件内容",
        system="test system",
        hooks=chapter.Hooks(),
        save_message=session.append_message,
    )

    assert [message["role"] for message in requested_contexts[0]] == ["user"]
    assert [message["role"] for message in requested_contexts[1]] == [
        "user",
        "assistant",
        "user",
    ]
    assert result[-1]["content"][0]["text"] == "完成"
