from types import SimpleNamespace

import s06_session_context.code as chapter


def test_store_round_trips_all_session_entry_types(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message({"role": "user", "content": "旧任务"})
    session.append_compaction(
        "## Goal\n继续实现会话",
        [{"role": "assistant", "content": [{"type": "text", "text": "最近进度"}]}],
    )
    session.append_custom("task_state", {"status": "doing"})

    entries = session.load_entries()

    assert [type(entry) for entry in entries] == [
        chapter.MessageEntry,
        chapter.CompactionEntry,
        chapter.CustomEntry,
    ]
    assert entries[1].summary == "## Goal\n继续实现会话"
    assert entries[2].data == {"status": "doing"}


def test_store_reads_s05_message_record_for_compatible_resume(tmp_path) -> None:
    path = tmp_path / "s05-session.jsonl"
    path.write_text(
        '{"type":"message","role":"user","content":"继续之前的任务"}\n',
        encoding="utf-8",
    )

    context = chapter.SessionManager.open(path).build_context()

    assert context == [{"role": "user", "content": "继续之前的任务"}]


def test_context_projection_uses_latest_compaction_and_skips_custom_entries() -> None:
    entries: list[chapter.SessionEntry] = [
        chapter.MessageEntry.from_message({"role": "user", "content": "很早的任务"}),
        chapter.CustomEntry("task_state", {"done": False}),
        chapter.CompactionEntry(
            summary="保留目标和关键决定",
            retained_tail=[{"role": "assistant", "content": "最近进度"}],
        ),
        chapter.MessageEntry.from_message({"role": "user", "content": "继续实现"}),
        chapter.CustomEntry("ignored", {"visible": False}),
    ]

    context = chapter.build_session_context(entries)

    assert context == [
        {"role": "user", "content": "[历史会话摘要，供继续任务]\n\n保留目标和关键决定"},
        {"role": "assistant", "content": "最近进度"},
        {"role": "user", "content": "继续实现"},
    ]


def test_context_projection_uses_only_the_latest_compaction() -> None:
    entries: list[chapter.SessionEntry] = [
        chapter.CompactionEntry("旧摘要", []),
        chapter.MessageEntry.from_message({"role": "user", "content": "旧尾部"}),
        chapter.CompactionEntry("新摘要", []),
        chapter.MessageEntry.from_message({"role": "user", "content": "新消息"}),
    ]

    context = chapter.build_session_context(entries)

    assert [message["content"] for message in context] == [
        "[历史会话摘要，供继续任务]\n\n新摘要",
        "新消息",
    ]


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
