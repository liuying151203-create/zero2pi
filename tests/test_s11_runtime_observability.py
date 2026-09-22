import json
from pathlib import Path

import s11_runtime_observability.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


def test_runtime_paths_are_scoped_to_s11() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s11")
    assert chapter.TRACE_ROOT == Path(".traces/s11")


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
