from types import SimpleNamespace

import s07_session_compaction.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


def test_system_prefers_ranged_reads_and_does_not_retry_denied_shell_commands() -> None:
    assert "大文件用 read_file 分段读取" in chapter.SYSTEM
    assert "工具被拒绝后不要换等价命令重试" in chapter.SYSTEM
    assert "start_line 和 limit" not in chapter.SYSTEM
    read_tool = next(tool for tool in chapter.TOOLS if tool["name"] == "read_file")
    assert read_tool["input_schema"]["properties"]["start_line"]["minimum"] == 1


def test_store_round_trips_message_and_compaction_entries(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    tail = _message("user", "保留的最新任务")

    session.append_message(_message("user", "较早任务"))
    session.append_compaction("已完成早期调查。", [tail])

    entries = session.load_entries()

    assert [type(entry) for entry in entries] == [chapter.MessageEntry, chapter.CompactionEntry]
    compaction = entries[1]
    assert isinstance(compaction, chapter.CompactionEntry)
    assert compaction.summary == "已完成早期调查。"
    assert list(compaction.retained_tail) == [tail]


def test_context_uses_only_latest_compaction_and_later_messages() -> None:
    old = chapter.MessageEntry.from_message(_message("user", "不应进入模型上下文的旧消息"))
    tail = chapter.MessageEntry.from_message(_message("assistant", "保留的最近回答"))
    compaction = chapter.CompactionEntry.create("旧消息摘要", [tail.message])
    later = chapter.MessageEntry.from_message(_message("user", "压缩后的新任务"))

    context = chapter.build_session_context([old, tail, compaction, later])

    assert context == [
        _message("user", "[会话摘要]\n旧消息摘要"),
        tail.message,
        later.message,
    ]


def test_compactor_persists_summary_and_recent_tail(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    for index in range(5):
        session.append_message(_message("user", f"第 {index} 条消息：" + "较长内容" * 20))

    summarized: list[list[chapter.Message]] = []

    def summarize(messages, previous_summary):
        assert previous_summary is None
        summarized.append(list(messages))
        return "已总结较早的三条消息。"

    compactor = chapter.ContextCompactor(
        session,
        chapter.CompactionPolicy(max_context_chars=500, keep_recent_chars=220),
        summarize,
    )

    outcome = compactor.compact_if_needed()

    assert outcome is not None
    assert outcome.summary_kind == "model"
    assert summarized[0]
    estimate_context = chapter.estimate_context_chars(session.build_context())
    assert estimate_context <= 500
    assert isinstance(session.load_entries()[-1], chapter.CompactionEntry)


def test_compactor_keeps_tool_use_and_result_together() -> None:
    messages = [
        _message("user", "较早任务"),
        _message(
            "assistant",
            [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}],
        ),
        _message("user", [{"type": "tool_result", "tool_use_id": "tool-1", "content": "内容"}]),
    ]

    tail_budget = chapter.estimate_context_chars(messages[1:])
    messages_to_summarize, retained_tail = chapter.split_context_for_compaction(
        messages,
        tail_budget,
    )

    assert messages_to_summarize == [messages[0]]
    assert retained_tail == messages[1:]


def test_prepare_compaction_combines_previous_tail_and_later_messages() -> None:
    previous_tail = _message("assistant", "上次压缩时保留的回答" * 20)
    later_message = _message("user", "上次压缩后新增的任务" * 20)
    entries: list[chapter.SessionEntry] = [
        chapter.MessageEntry.from_message(_message("user", "已进入旧摘要的消息")),
        chapter.CompactionEntry.create("旧摘要", [previous_tail]),
        chapter.MessageEntry.from_message(later_message),
    ]

    plan = chapter.prepare_compaction(
        entries,
        chapter.CompactionPolicy(max_context_chars=300, keep_recent_chars=100),
    )

    assert plan is not None
    assert plan.previous_summary == "旧摘要"
    assert list(plan.messages_to_summarize) == [previous_tail, later_message]
    assert list(plan.retained_tail) == []


def test_request_wrapper_compacts_then_uses_latest_context(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message(_message("user", "第一条需要压缩的内容" * 30))
    session.append_message(_message("user", "第二条需要保留的内容"))
    requested_contexts: list[list[chapter.Message]] = []

    def create_message(**kwargs):
        requested_contexts.append(kwargs["messages"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    compactor = chapter.ContextCompactor(
        session,
        chapter.CompactionPolicy(max_context_chars=300, keep_recent_chars=100),
        lambda messages, previous_summary: "第一条摘要",
    )
    chapter.CompactedContextRequester(
        create_message,
        session,
        compactor,
    )(
        messages=[_message("user", "不应使用这条内存消息")],
    )

    assert requested_contexts == [
        [
            _message("user", "[会话摘要]\n第一条摘要"),
            _message("user", "第二条需要保留的内容"),
        ]
    ]


def test_compactor_bounds_huge_tool_result_and_converges(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message(
        _message(
            "assistant",
            [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}],
        )
    )
    session.append_message(
        _message(
            "user",
            [{"type": "tool_result", "tool_use_id": "tool-1", "content": "x" * 20_000}],
        )
    )
    session.append_message(_message("user", "继续当前任务"))
    summarized: list[list[chapter.Message]] = []

    def summarize(messages, previous_summary):
        assert previous_summary is None
        summarized.append(list(messages))
        return "已读取大文件，继续当前任务。"

    compactor = chapter.ContextCompactor(
        session,
        chapter.CompactionPolicy(max_context_chars=1000, keep_recent_chars=300),
        summarize,
    )

    outcome = compactor.compact_if_needed()

    assert outcome is not None
    assert outcome.after_chars <= 1000
    assert summarized[0][1]["content"][0]["content"] == "x" * 20_000
    assert session.build_context()[-1] == _message("user", "继续当前任务")


def test_next_compaction_updates_previous_summary_without_resummarizing_it(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message(_message("user", "第一阶段任务" * 30))
    session.append_compaction("第一阶段摘要", [])
    session.append_message(_message("user", "第二阶段任务" * 60))
    calls: list[tuple[list[chapter.Message], str | None]] = []

    def summarize(messages, previous_summary):
        calls.append((list(messages), previous_summary))
        return "第一阶段摘要；第二阶段任务进行中。"

    compactor = chapter.ContextCompactor(
        session,
        chapter.CompactionPolicy(max_context_chars=300, keep_recent_chars=100),
        summarize,
    )

    compactor.compact_if_needed()

    assert calls[0][1] == "第一阶段摘要"
    assert all("[会话摘要]" not in str(message["content"]) for message in calls[0][0])


def test_compactor_uses_safe_fallback_when_summary_is_unavailable(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message(_message("user", "较早任务" * 80))
    session.append_message(_message("user", "最新任务"))
    compactor = chapter.ContextCompactor(
        session,
        chapter.CompactionPolicy(max_context_chars=300, keep_recent_chars=100),
        lambda messages, previous_summary: None,
    )

    outcome = compactor.compact_if_needed()
    context = session.build_context()

    assert outcome is not None
    assert outcome.summary_kind == "fallback"
    assert str(session.path) in str(context[0]["content"])
    assert "较早任务" in str(context[0]["content"])
    assert context[1] == _message("user", "最新任务")


def test_context_summarizer_uses_a_separate_tool_free_request() -> None:
    requests: list[dict[str, object]] = []

    def create_message(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="摘要结果")])

    assert chapter.summarize_context(
        [_message("user", "原始任务")],
        create_message,
        previous_summary="已有摘要",
        max_tokens=300,
    ) == "摘要结果"
    assert requests[0]["system"] == chapter.COMPACTION_SYSTEM
    assert requests[0]["tools"] == []
    assert requests[0]["max_tokens"] == 300
    assert "原始任务" in str(requests[0]["messages"])
    assert "已有摘要" in str(requests[0]["messages"])


def test_summary_serialization_truncates_large_tool_results() -> None:
    transcript = chapter.serialize_context_for_summary(
        [
            _message(
                "user",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "x" * 20_000}],
            )
        ]
    )

    assert "工具结果已截断" in transcript
    assert len(transcript) < 3000


def test_context_summarizer_retries_with_more_tokens_for_thinking_only_response() -> None:
    requests: list[dict[str, object]] = []
    responses = [
        SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="正在整理")]),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="重试后的摘要")]),
    ]

    def create_message(**kwargs):
        requests.append(kwargs)
        return responses.pop(0)

    assert chapter.summarize_context(
        [_message("user", "原始任务")],
        create_message,
        max_tokens=300,
    ) == "重试后的摘要"
    assert [request["max_tokens"] for request in requests] == [300, 1024]
