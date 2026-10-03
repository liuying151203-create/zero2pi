from pathlib import Path
from types import SimpleNamespace

import s10_model_provider.code as chapter


def _message(role: str, content: object) -> chapter.Message:
    return {"role": role, "content": content}


class FakeAnthropicStream:
    def __init__(self, chunks: list[str], final_message: object):
        self.text_stream = iter(chunks)
        self.final_message = final_message

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def get_final_message(self):
        return self.final_message


def test_default_session_root_is_scoped_to_s10() -> None:
    assert chapter.SESSION_ROOT == Path(".sessions/s10")


def test_summary_options_follow_api_type_not_model_name() -> None:
    anthropic = chapter.AnthropicProvider(client=None, model="deepseek-flash")
    compatible = chapter.OpenAIChatProvider(client=None, model="deepseek-flash")
    assert chapter.summary_request_options(anthropic) == {"thinking": {"type": "disabled"}}
    assert chapter.summary_request_options(compatible) == {}


def test_anthropic_provider_normalizes_response_and_streams_text() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="你好")],
        model="claude-test",
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=3,
            cache_read_input_tokens=4,
            cache_creation_input_tokens=2,
        ),
    )

    class Messages:
        def create(self, **kwargs):
            return response

        def stream(self, **kwargs):
            return FakeAnthropicStream(["你", "好"], response)

    provider = chapter.AnthropicProvider(
        client=SimpleNamespace(messages=Messages()),
        model="fallback",
    )
    chunks: list[str] = []

    complete = provider.complete(messages=[], system="test", tools=[], max_tokens=10)
    streamed = provider.stream(
        messages=[],
        system="test",
        tools=[],
        max_tokens=10,
        on_text=chunks.append,
    )

    assert complete == streamed
    assert complete.content == [{"type": "text", "text": "你好"}]
    assert complete.usage.total_tokens == 29
    assert chunks == ["你", "好"]


def test_openai_provider_converts_internal_messages_and_tools() -> None:
    captured: list[dict[str, object]] = []
    response = SimpleNamespace(
        model="deepseek-chat",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="我来读取",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments='{"path":"README.md"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=40, completion_tokens=6),
    )

    class Completions:
        def create(self, **kwargs):
            captured.append(kwargs)
            return response

    provider = chapter.OpenAIChatProvider(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model="deepseek-chat",
    )
    messages = [
        _message("user", "读取说明"),
        _message(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "old-call",
                    "name": "read_file",
                    "input": {"path": "AGENTS.md"},
                }
            ],
        ),
        _message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "old-call",
                    "content": "文件内容",
                }
            ],
        ),
    ]

    result = provider.complete(
        system="系统提示词",
        messages=messages,
        tools=[
            {
                "name": "read_file",
                "description": "读取文件",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        max_tokens=100,
    )

    sent_messages = captured[0]["messages"]
    assert sent_messages[0] == {"role": "system", "content": "系统提示词"}
    assert sent_messages[-1] == {
        "role": "tool",
        "tool_call_id": "old-call",
        "content": "文件内容",
    }
    assert captured[0]["tools"][0]["function"]["name"] == "read_file"
    assert result.content[1] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "read_file",
        "input": {"path": "README.md"},
    }
    assert result.usage.total_tokens == 46


def test_openai_stream_assembles_tool_arguments_and_usage() -> None:
    chunks = [
        SimpleNamespace(
            model="deepseek-chat",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(content="读取", tool_calls=None),
                )
            ],
        ),
        SimpleNamespace(
            model="deepseek-chat",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-1",
                                function=SimpleNamespace(
                                    name="read_file",
                                    arguments='{"path":',
                                ),
                            )
                        ],
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="deepseek-chat",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name=None,
                                    arguments='"README.md"}',
                                ),
                            )
                        ],
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="deepseek-chat",
            usage=SimpleNamespace(
                prompt_tokens=30,
                completion_tokens=5,
                prompt_tokens_details=SimpleNamespace(cached_tokens=10),
            ),
            choices=[],
        ),
    ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            assert kwargs["stream_options"] == {"include_usage": True}
            assert "tools" not in kwargs
            return iter(chunks)

    provider = chapter.OpenAIChatProvider(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model="deepseek-chat",
    )
    text_chunks: list[str] = []

    result = provider.stream(
        system="test",
        messages=[],
        tools=[],
        max_tokens=100,
        on_text=text_chunks.append,
    )

    assert text_chunks == ["读取"]
    assert result.content == [
        {"type": "text", "text": "读取"},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_tokens == 10
    assert result.usage.total_tokens == 35


def test_requesters_use_provider_and_keep_streaming_events() -> None:
    response = chapter.ModelResponse(
        content=[{"type": "text", "text": "你好"}],
        model="test-model",
        stop_reason="stop",
        usage=chapter.ModelUsage(input_tokens=2, output_tokens=1),
    )

    class Provider:
        model = "test-model"

        def complete(self, **kwargs):
            return response

        def stream(self, *, on_text, **kwargs):
            on_text("你")
            on_text("好")
            return response

    events: list[chapter.AgentEvent] = []
    blocking = chapter.BlockingModelRequester(Provider(), 12, events.append)
    streaming = chapter.StreamingModelRequester(Provider(), 12, events.append)

    assert blocking(messages=[]) is response
    assert streaming(messages=[]) is response
    assert [event.type for event in events] == [
        "model_request",
        "model_request",
        "assistant_delta",
        "assistant_delta",
    ]


def test_agent_loop_executes_tool_from_normalized_response() -> None:
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
            model="test",
            stop_reason="tool_use",
            usage=chapter.ModelUsage(),
        ),
        chapter.ModelResponse(
            content=[{"type": "text", "text": "读取完成"}],
            model="test",
            stop_reason="stop",
            usage=chapter.ModelUsage(),
        ),
    ]
    events: list[chapter.AgentEvent] = []

    result = chapter.agent_loop(
        [_message("user", "读取说明")],
        create_message=lambda **kwargs: responses.pop(0),
        dispatch=lambda name, arguments: "文件内容",
        system="test",
        hooks=chapter.Hooks(),
        emit=events.append,
    )

    assert result[-1]["content"][0]["text"] == "读取完成"
    assert [event.type for event in events] == [
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
        "agent_end",
    ]
