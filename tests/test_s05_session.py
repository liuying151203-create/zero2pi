from types import SimpleNamespace

import pytest

import s05_session.code as chapter


def test_session_store_round_trips_unicode_and_tool_blocks(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    store = chapter.JsonlSessionStore(path)
    message = chapter.SessionMessage(
        role="assistant",
        content=[{"type": "text", "text": "你好"}],
    )

    store.append(message)
    loaded = store.read_all()

    assert loaded[0].to_message() == {
        "role": "assistant",
        "content": [{"type": "text", "text": "你好"}],
    }
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_session_manager_converts_sdk_objects_before_persisting(tmp_path) -> None:
    manager = chapter.SessionManager.open(tmp_path / "session.jsonl")
    manager.append_message(
        {
            "role": "assistant",
            "content": [SimpleNamespace(type="text", text="完成")],
        }
    )

    assert manager.load_messages() == [
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]}
    ]


def test_store_reports_corrupted_line_number(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"message","role":"user","content":"ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        chapter.JsonlSessionStore(path).read_all()


def test_agent_loop_persists_assistant_and_tool_messages() -> None:
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
    saved: list[chapter.Message] = []

    def create_message(**kwargs):
        return responses.pop(0)

    messages = [{"role": "user", "content": "read a file"}]
    result = chapter.agent_loop(
        messages,
        create_message=create_message,
        dispatch=lambda name, arguments: "file content",
        system="test system",
        hooks=chapter.Hooks(),
        save_message=saved.append,
    )

    assert [message["role"] for message in saved] == ["assistant", "user", "assistant"]
    assert saved[0]["content"][0]["type"] == "tool_use"
    assert saved[1]["content"][0]["type"] == "tool_result"
    assert result[-1]["content"][0]["text"] == "done"
