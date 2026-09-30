import json
from pathlib import Path

import pytest

import s11_runtime_observability.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


def test_runtime_paths_are_scoped_to_s11() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s11")
    assert chapter.TRACE_ROOT == Path(".traces/s11")


def test_incomplete_saved_usage_is_rejected_instead_of_filled_with_zero() -> None:
    with pytest.raises(ValueError, match="usage 缺少"):
        chapter._usage_from_record({"input_tokens": 10})


def test_s11_treats_s10_zero_placeholder_as_unknown_usage() -> None:
    assert chapter._known_usage(chapter.ModelUsage()) is None
    known = chapter.ModelUsage(input_tokens=1)
    assert chapter._known_usage(known) is known


def test_event_dispatcher_sends_same_event_to_all_listeners() -> None:
    first: list[chapter.AgentEvent] = []
    second: list[chapter.AgentEvent] = []
    events = chapter.EventDispatcher()
    events.subscribe(first.append)
    events.subscribe(second.append)

    event = chapter.AgentEvent(type="agent_start", data={"message_count": 1})
    events.emit(event)

    assert first == [event]
    assert second == [event]


def test_blocking_requester_reports_final_usage() -> None:
    events: list[chapter.AgentEvent] = []
    response = chapter.ModelResponse(
        content=[],
        model="summary-model",
        stop_reason="end_turn",
        usage=chapter.ModelUsage(
            input_tokens=30,
            output_tokens=8,
            cache_read_tokens=4,
            cache_write_tokens=2,
        ),
    )

    class Provider:
        model = "test-model"

        def complete(self, **kwargs):
            return response

    requester = chapter.BlockingModelRequester(
        provider=Provider(),
        timeout_seconds=12,
        emit=events.append,
    )

    assert requester(messages=[], system="summary", tools=[], max_tokens=100) is response
    assert [event.type for event in events] == ["model_request", "model_response"]
    assert events[1].data["purpose"] == "summary"
    assert events[1].data["usage"]["total_tokens"] == 44


def test_streaming_requester_counts_usage_only_on_final_response() -> None:
    events: list[chapter.AgentEvent] = []
    response = chapter.ModelResponse(
        content=[{"type": "text", "text": "你好"}],
        usage=chapter.ModelUsage(input_tokens=10, output_tokens=2),
        model="assistant-model",
        stop_reason="end_turn",
    )

    class Provider:
        model = "test-model"

        def stream(self, *, on_text, **kwargs):
            on_text("你")
            on_text("好")
            return response

    requester = chapter.StreamingModelRequester(
        provider=Provider(),
        timeout_seconds=12,
        emit=events.append,
    )
    requester(messages=[], system="test", tools=[], max_tokens=100)

    assert [event.type for event in events] == [
        "model_request",
        "assistant_delta",
        "assistant_delta",
        "model_response",
    ]
    assert sum(event.type == "model_response" for event in events) == 1
    assert events[-1].data["usage"]["total_tokens"] == 12


def test_usage_tracker_resets_and_aggregates_events() -> None:
    tracker = chapter.UsageTracker()
    tracker(chapter.AgentEvent(type="agent_start", data={"message_count": 2}))
    tracker(
        chapter.AgentEvent(
            type="model_request",
            data={"model": "m", "purpose": "assistant", "timeout_seconds": 10},
        )
    )
    tracker(
        chapter.AgentEvent(
            type="model_response",
            data={
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cache_read_tokens": 3,
                    "cache_write_tokens": 2,
                }
            },
        )
    )
    tracker(
        chapter.AgentEvent(
            type="tool_call",
            data={"tool_call_id": "t1", "name": "read_file", "arguments": {}},
        )
    )
    tracker(
        chapter.AgentEvent(
            type="tool_result",
            data={
                "tool_call_id": "t1",
                "name": "read_file",
                "output": "ok",
                "is_error": False,
                "duration_ms": 7,
            },
        )
    )
    tracker(chapter.AgentEvent(type="agent_end", data={"messages": []}))

    assert tracker.stats.model_requests == 1
    assert tracker.stats.tool_calls == 1
    assert tracker.stats.total_tokens == 30
    assert tracker.stats.tool_duration_ms == 7
    assert "Token 30" in tracker.summary_text()


def test_trace_recorder_keeps_compact_tool_result_and_skips_deltas(tmp_path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    recorder = chapter.JsonlTraceRecorder(trace_path)

    recorder(chapter.AgentEvent(type="assistant_delta", data={"text": "不落盘"}))
    recorder(
        chapter.AgentEvent(
            type="tool_result",
            data={
                "tool_call_id": "t1",
                "name": "read_file",
                "output": "x" * 800,
                "is_error": False,
                "duration_ms": 3.5,
            },
        )
    )

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["data"]["output_chars"] == 800
    assert len(records[0]["data"]["output_preview"]) == 500


def test_agent_loop_emits_trackable_tool_lifecycle() -> None:
    responses = [
        chapter.ModelResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
            model="test-model",
            stop_reason="tool_use",
            usage=chapter.ModelUsage(),
        ),
        chapter.ModelResponse(
            content=[{"type": "text", "text": "完成"}],
            model="test-model",
            stop_reason="stop",
            usage=chapter.ModelUsage(),
        ),
    ]
    events: list[chapter.AgentEvent] = []

    chapter.agent_loop(
        [_message("user", "读取说明")],
        create_message=lambda **kwargs: responses.pop(0),
        dispatch=lambda name, arguments: "文件内容",
        system="test",
        hooks=chapter.Hooks(),
        emit=events.append,
    )

    assert [event.type for event in events] == [
        "agent_start",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "agent_end",
    ]
    assert events[2].data["tool_call_id"] == "tool-1"
    assert events[3].data["tool_call_id"] == "tool-1"
    assert events[3].data["is_error"] is False


def test_session_usage_survives_reload_without_entering_model_context(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    session = chapter.SessionManager.open(path)
    session.append_message(_message("user", "旧问题"))
    session.append_assistant(
        _message("assistant", [{"type": "text", "text": "旧回答"}]),
        chapter.ModelResponse(
            content=[{"type": "text", "text": "旧回答"}],
            model="deepseek-chat",
            stop_reason="end_turn",
            usage=chapter.ModelUsage(input_tokens=100, output_tokens=20),
        ),
    )
    session.append_compaction(
        "旧对话摘要",
        [],
        summary_model="deepseek-chat",
        summary_usage=chapter.ModelUsage(input_tokens=60, output_tokens=10),
    )
    session.append_model_error("deepseek-chat", "summary", None)
    session.append_message(_message("user", "继续"))
    session.append_assistant(
        _message("assistant", [{"type": "text", "text": "已继续"}]),
        chapter.ModelResponse(
            content=[{"type": "text", "text": "已继续"}],
            model="deepseek-chat",
            stop_reason="end_turn",
            usage=chapter.ModelUsage(input_tokens=80, output_tokens=15),
        ),
    )

    reopened = chapter.SessionManager.open(path)
    stats = reopened.session_stats()
    assert (stats.model_requests, stats.summary_requests) == (4, 2)
    assert stats.known_tokens == 285
    assert stats.unknown_usage_requests == 1
    context = reopened.build_context()
    assert context[0]["content"] == "[会话摘要]\n旧对话摘要"
    assert [message["role"] for message in context] == ["user", "user", "assistant"]
    assert all("usage" not in message for message in context)
    assert all("model" not in message for message in context)


def test_failed_response_with_usage_is_saved_on_assistant_message(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "failed.jsonl")
    response = chapter.ModelResponse(
        content=[{"type": "text", "text": "未完成"}],
        model="test-model",
        stop_reason="error",
        usage=chapter.ModelUsage(input_tokens=20, output_tokens=3),
    )
    dispatched: list[str] = []
    chapter.agent_loop(
        [_message("user", "执行")],
        create_message=lambda **kwargs: response,
        dispatch=lambda name, arguments: dispatched.append(name) or "ok",
        system="test",
        hooks=chapter.Hooks(),
        emit=lambda event: None,
        save_message=session.append_message,
        save_assistant=session.append_assistant,
    )

    assert dispatched == []
    assert session.session_stats().failed_requests == 1
    assert session.session_stats().known_tokens == 23
    record = json.loads(session.path.read_text(encoding="utf-8").splitlines()[0])
    assert record["type"] == "message"
    assert record["stop_reason"] == "error"
    assert record["usage"]["total_tokens"] == 23


def test_exception_usage_is_recorded_even_without_assistant_message(tmp_path) -> None:
    class MeteredError(RuntimeError):
        usage = chapter.ModelUsage(input_tokens=11, output_tokens=4)

    class Provider:
        model = "test-model"

        def stream(self, *, on_text, **kwargs):
            raise MeteredError("中途断开")

    session = chapter.SessionManager.open(tmp_path / "failed-call.jsonl")
    tracker = chapter.UsageTracker()
    events = chapter.EventDispatcher()
    events.subscribe(tracker)
    events.subscribe(chapter.SessionErrorRecorder(session))
    tracker(chapter.AgentEvent(type="agent_start", data={"message_count": 1}))
    requester = chapter.StreamingModelRequester(Provider(), 10, events.emit)

    try:
        requester(messages=[])
    except MeteredError:
        pass
    else:
        raise AssertionError("应抛出模型请求错误")

    assert tracker.stats.model_requests == 1
    assert tracker.stats.failed_requests == 1
    assert tracker.stats.total_tokens == 15
    reopened = chapter.SessionManager.open(session.path)
    assert reopened.session_stats().known_tokens == 15
    assert reopened.session_stats().failed_requests == 1
    assert reopened.build_context() == []


def test_compaction_records_successful_summary_and_failed_attempt_separately(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "compact.jsonl")
    session.append_message(_message("user", "较早需求" * 80))
    session.append_message(_message("user", "当前需求" * 5))
    responses = [
        chapter.ModelResponse(
            content=[],
            model="test-model",
            stop_reason="max_tokens",
            usage=chapter.ModelUsage(input_tokens=35, output_tokens=5),
        ),
        chapter.ModelResponse(
            content=[{"type": "text", "text": "较早需求已总结"}],
            model="test-model",
            stop_reason="end_turn",
            usage=chapter.ModelUsage(input_tokens=40, output_tokens=8),
        ),
    ]
    summarizer = chapter.ContextSummarizer(
        create_message=lambda **kwargs: responses.pop(0),
        max_tokens=256,
        session=session,
        emit=lambda event: None,
    )
    compactor = chapter.ContextCompactor(
        session=session,
        policy=chapter.CompactionPolicy(max_context_chars=300, keep_recent_chars=80),
        summarize=summarizer,
    )

    outcome = compactor.compact_if_needed()

    assert outcome is not None
    assert outcome.summary_kind == "model"
    entries = session.load_entries()
    assert isinstance(entries[-2], chapter.ModelErrorEntry)
    entry = entries[-1]
    assert isinstance(entry, chapter.CompactionEntry)
    assert entry.summary_usage == chapter.ModelUsage(input_tokens=40, output_tokens=8)
    assert chapter.SessionManager.open(session.path).session_stats().known_tokens == 88
    assert chapter.SessionManager.open(session.path).session_stats().summary_requests == 2


def test_summary_retry_exception_does_not_lose_first_attempt_usage(tmp_path) -> None:
    class MeteredError(RuntimeError):
        usage = chapter.ModelUsage(input_tokens=7, output_tokens=1)

    class Provider:
        model = "test-model"
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return chapter.ModelResponse(
                    content=[],
                    model=self.model,
                    stop_reason="max_tokens",
                    usage=chapter.ModelUsage(input_tokens=30, output_tokens=5),
                )
            raise MeteredError("摘要重试中断")

    session = chapter.SessionManager.open(tmp_path / "summary-failure.jsonl")
    tracker = chapter.UsageTracker()
    events = chapter.EventDispatcher()
    events.subscribe(tracker)
    events.subscribe(chapter.SessionErrorRecorder(session))
    summarizer = chapter.ContextSummarizer(
        create_message=chapter.BlockingModelRequester(Provider(), 10, events.emit),
        max_tokens=256,
        session=session,
        emit=events.emit,
    )
    tracker(chapter.AgentEvent(type="agent_start", data={"message_count": 1}))

    try:
        summarizer([_message("user", "需求")], None)
    except MeteredError:
        pass
    else:
        raise AssertionError("摘要重试应抛出模型错误")

    stats = chapter.SessionManager.open(session.path).session_stats()
    assert (stats.model_requests, stats.summary_requests, stats.failed_requests) == (2, 2, 2)
    assert stats.known_tokens == 43
    assert tracker.stats.total_tokens == 43
    assert tracker.stats.failed_requests == 2


def test_error_summary_text_is_not_saved_as_compaction(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "error-summary.jsonl")
    response = chapter.ModelResponse(
        content=[{"type": "text", "text": "不完整摘要"}],
        model="test-model",
        stop_reason="error",
        usage=chapter.ModelUsage(input_tokens=12, output_tokens=2),
    )
    summarizer = chapter.ContextSummarizer(
        create_message=lambda **kwargs: response,
        max_tokens=1024,
        session=session,
        emit=lambda event: None,
    )

    result = summarizer([_message("user", "旧问题")], None)

    assert result.text is None
    assert isinstance(session.load_entries()[0], chapter.ModelErrorEntry)
    assert session.session_stats().known_tokens == 14


def test_older_s11_records_remain_readable_but_are_marked_untracked(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "old.jsonl")
    session.append_message(_message("assistant", [{"type": "text", "text": "旧回答"}]))

    reopened = chapter.SessionManager.open(session.path)
    assert reopened.build_context()[0]["content"][0]["text"] == "旧回答"
    assert reopened.session_stats().legacy_entries == 1
    assert "旧记录 1 条未统计" in reopened.session_stats().summary_text()


def test_session_stats_recounts_tool_calls_from_saved_assistant_messages(tmp_path) -> None:
    session = chapter.SessionManager.open(tmp_path / "tool-session.jsonl")
    session.append_assistant(
        _message(
            "assistant",
            [{"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}}],
        ),
        chapter.ModelResponse(
            content=[],
            model="test-model",
            stop_reason="tool_use",
            usage=chapter.ModelUsage(input_tokens=8, output_tokens=2),
        ),
    )
    session.append_message(
        _message("user", [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}])
    )

    assert chapter.SessionManager.open(session.path).session_stats().tool_calls == 1
