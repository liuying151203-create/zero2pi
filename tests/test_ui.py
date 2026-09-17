from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)


def test_format_tool_call_shows_name_and_arguments() -> None:
    result = format_tool_call("read_file", {"path": "README.md"})
    assert "工具  🔧 read_file" in result
    assert '"path": "README.md"' in result


def test_format_tool_result_truncates_long_output() -> None:
    result = format_tool_result("abcdef", limit=3)
    assert "结果  ↳ abc" in result
    assert "结果已截断" in result


def test_format_labels_are_distinguishable_without_terminal_color() -> None:
    assert format_user_prompt("s02").startswith("s02 >>")
    assert format_assistant_message("完成").startswith("Agent  完成")
    assert format_error("failed").startswith("错误  failed")
