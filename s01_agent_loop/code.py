#!/usr/bin/env python3
"""s01：最小可用的 Agent 核心循环。

循环流程为：用户消息 → 模型 → tool_use → 执行工具 → tool_result → 模型。
模型负责决定是否调用工具，Harness 负责执行工具并把结果放回消息历史，
直到模型返回不包含工具调用的最终回答。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

Message = dict[str, Any]
CreateMessage = Callable[..., Any]
ExecuteTool = Callable[[str], str]

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current project directory.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
]


def run_bash(command: str, *, cwd: str | None = None) -> str:
    """执行一条 shell 命令并返回标准输出和错误输出。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except OSError as error:
        return f"Error: {error}"

    output = (result.stdout + result.stderr).strip()
    return output or "(no output)"


def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def agent_loop(
    messages: list[Message],
    *,
    create_message: CreateMessage,
    execute_tool: ExecuteTool,
    system: str,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行 Agent 的最小核心循环。

    作用：让模型能够看到工具执行结果，并在需要时继续推理，而不是在
    第一次返回工具调用后就结束。这个函数只负责循环和消息历史，不负责
    具体的模型实现或工具实现，二者通过参数注入，便于替换和测试。

    输入：
        messages：已有的对话消息列表，会在原列表上追加 assistant 和 tool_result 消息。
        create_message：创建一次模型响应的函数，接收 system、messages、tools 和 max_tokens。
        execute_tool：执行单个工具调用的函数，本章接收命令字符串并返回文本结果。
        system：发送给模型的系统提示词。
        tools：提供给模型的工具定义；未传入时使用本章唯一的 bash 工具。
        max_tokens：单次模型响应的最大 token 数。

    输出：
        返回追加了完整执行过程的同一个消息列表。当模型响应中不再包含
        tool_use 时停止循环，此时最后一条 assistant 消息就是最终回答。

    流程：
        1. 请求模型并追加 assistant 消息。
        2. 检查响应中是否包含 tool_use。
        3. 没有工具调用则结束；有则逐个执行并收集 tool_result。
        4. 将工具结果作为 user 消息追加，回到第 1 步。
    """
    while True:
        response = create_message(
            system=system,
            messages=messages,
            tools=tools or TOOLS,
            max_tokens=max_tokens,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [
            block
            for block in response.content
            if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            input_data = _get(block, "input") or {}
            output = execute_tool(input_data["command"])
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": results})


def _text_from_content(content: Any) -> str:
    """提取模型响应中的文本块，用于终端展示。"""
    if isinstance(content, str):
        return content

    return "\n".join(
        str(text)
        for block in content
        if _get(block, "type") == "text"
        if (text := _get(block, "text"))
    )


def main() -> None:
    """启动第一章的交互式终端 Agent。

    作用：读取本地 `.env` 中的模型配置，创建 Anthropic 客户端，接收用户
    输入并交给 `agent_loop`。本方法只负责终端交互和依赖组装，核心循环
    本身保持在 `agent_loop` 中，方便后续章节继续复用和扩展。

    输入：用户在终端输入的自然语言任务。
    输出：模型最终返回的文本；输入空行、`q` 或 `exit` 时退出程序。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("Set MODEL_ID in .env before running s01_agent_loop.")

    client_options: dict[str, str] = {}
    if api_key := os.getenv("ANTHROPIC_API_KEY"):
        client_options["api_key"] = api_key
    if base_url := os.getenv("ANTHROPIC_BASE_URL"):
        client_options["base_url"] = base_url
    client = Anthropic(**client_options)
    system = f"You are a coding agent working in {os.getcwd()}. Use bash to solve tasks."

    def create_message(**kwargs: Any) -> Any:
        return client.messages.create(model=model, **kwargs)

    print("s01：核心循环")
    print("输入任务，输入 q 退出。\n")

    history: list[Message] = []
    while True:
        try:
            query = input("s01 >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        history.append({"role": "user", "content": query})
        agent_loop(
            history,
            create_message=create_message,
            execute_tool=run_bash,
            system=system,
        )
        print(_text_from_content(history[-1]["content"]))
        print()


if __name__ == "__main__":
    main()
