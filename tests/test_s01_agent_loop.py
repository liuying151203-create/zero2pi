from types import SimpleNamespace

from s01_agent_loop.code import agent_loop


def test_loop_feeds_tool_result_back_until_model_stops() -> None:
    responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    input={"command": "echo hello"},
                )
            ]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")]),
    ]
    calls: list[dict] = []

    def create_message(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    messages = [{"role": "user", "content": "say hello"}]
    result = agent_loop(
        messages,
        create_message=create_message,
        execute_tool=lambda command: f"output for: {command}",
        system="test system",
    )

    assert len(calls) == 2
    assert result[-1]["content"][0].text == "done"
    assert result[-2]["role"] == "user"
    assert result[-2]["content"][0]["type"] == "tool_result"
    assert result[-2]["content"][0]["content"] == "output for: echo hello"


def test_loop_stops_when_model_does_not_call_a_tool() -> None:
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="final")])
    calls = 0

    def create_message(**kwargs):
        nonlocal calls
        calls += 1
        return response

    result = agent_loop(
        [{"role": "user", "content": "answer directly"}],
        create_message=create_message,
        execute_tool=lambda command: "unreachable",
        system="test system",
    )

    assert calls == 1
    assert result[-1]["content"][0].text == "final"
