"""终端输出格式化工具。

本模块只负责展示，不参与 Agent 决策、工具分发或消息历史处理。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _paint(text: str, code: str) -> str:
    """在支持颜色的终端中给文本添加 ANSI 样式。"""
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def format_user_prompt(name: str) -> str:
    """生成用户输入提示符。"""
    return _paint(f"{name} >> ", "36")


def format_model_request(timeout_seconds: float) -> str:
    """生成模型请求状态提示。"""
    return _paint(f"模型  正在请求模型（超时 {timeout_seconds:g} 秒）...", "90")


def format_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """格式化工具名称和调用参数。"""
    payload = json.dumps(arguments, ensure_ascii=False, default=str)
    return "\n".join(
        (
            _paint(f"工具  🔧 {name}", "33"),
            _paint(f"参数  {payload}", "33"),
        )
    )


def format_tool_result(output: str, limit: int = 200) -> str:
    """格式化工具结果，并截断过长输出。"""
    text = str(output)
    preview = text[:limit]
    if len(text) > limit:
        preview += "\n... (结果已截断)"

    lines = preview.splitlines() or ["(no output)"]
    rendered = [f"结果  ↳ {lines[0]}"]
    rendered.extend(f"        {line}" for line in lines[1:])
    return _paint("\n".join(rendered), "34")


def format_assistant_message(text: str) -> str:
    """格式化 Agent 的最终文本回答。"""
    content = text or "(no text)"
    lines = content.splitlines()
    rendered = [f"Agent  {lines[0]}"]
    rendered.extend(f"       {line}" for line in lines[1:])
    return _paint("\n".join(rendered), "32")


def format_error(message: str) -> str:
    """格式化错误信息。"""
    return _paint(f"错误  {message}", "31")
