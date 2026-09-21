#!/usr/bin/env python3
"""s02：工具调用与工具分发。

本章保留 s01 的核心循环，只把“执行一个 bash 命令”扩展为：

    tool_name + tool_arguments -> TOOL_HANDLERS -> tool_result

模型负责选择工具和生成参数，Harness 负责通过注册表找到处理函数并执行。
"""

from __future__ import annotations

import glob as glob_module
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from zero2pi.model import ModelRequester
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# ===== 来自 s01：消息类型与模型调用类型 =====
# s02 保留 s01 的消息结构，并新增工具处理函数和分发器类型。

Message = dict[str, Any]
ToolHandler = Callable[..., str]
DispatchTool = Callable[[str, dict[str, Any]], str]

WORKDIR = Path.cwd()

# ===== s02 修改：全局系统提示词 =====
# 在 s01 的全局提示词基础上，增加“优先使用专用工具”的约束。

SYSTEM = (
    f"你是运行在 {WORKDIR} 的编程 Agent。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。Windows 下使用 cmd.exe 命令。"
)


# ===== 来自 s01：bash 工具（保持） =====

def run_bash(command: str) -> str:
    """在工作目录执行 shell 命令。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
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


# ===== s02 新增：工作区路径与文件工具 =====

def safe_path(path: str) -> Path:
    """把相对路径解析到工作目录内，拒绝越界路径。"""
    # s02 新增：文件工具统一限制在工作区，避免路径参数直接访问外部文件。
    resolved = (WORKDIR / path).resolve()
    if not resolved.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_read(path: str, limit: int | None = None) -> str:
    """读取工作目录内的文本文件，可选限制行数。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error: {error}"


def run_write(path: str, content: str) -> str:
    """向工作目录内的文件写入文本，必要时创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"
    except (OSError, ValueError) as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中替换第一次出现的精确文本。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error: {error}"


def run_glob(pattern: str) -> str:
    """查找工作目录内匹配 glob 模式的文件，并返回相对路径。"""
    try:
        matches = sorted(
            {
                match
                for match in glob_module.glob(pattern, root_dir=WORKDIR, recursive=True)
                if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
            }
        )
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except (OSError, ValueError) as error:
        return f"Error: {error}"


# ===== s02 新增：工具定义 =====

# s02 新增：把 s01 的单个 bash 工具扩展为模型可选择的五个工具。
TOOLS = [
    {
        "name": "bash",
        "description": "Run one shell command in the current workspace. On Windows, use cmd.exe syntax.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text to a file in the current workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact text occurrence in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find workspace files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]

# ===== s02 新增：工具注册表 =====

# s02 新增：用名称到处理函数的映射替代核心循环中的工具分支，便于继续扩展工具。
TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ===== s02 新增：统一工具分发 =====

def dispatch_tool(name: str, arguments: dict[str, Any]) -> str:
    """根据工具名查找并执行对应的工具处理函数。

    作用：把模型返回的工具名称和参数转换成统一的 handler 调用，隔离核心循环
    与具体工具实现。

    输入：
        name：模型返回的工具名称。
        arguments：模型生成的 JSON 参数字典。

    输出：
        工具处理函数返回的结果文本；未知工具、参数错误或运行异常都会转换为
        可回传给模型的错误文本。

    流程：查找注册表 → 调用 handler → 捕获工具边界内的异常 → 返回统一文本结果。
    """
    # s02 新增：统一从注册表查找 handler，让 agent_loop 不依赖具体工具实现。
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool: {name}"

    try:
        return handler(**arguments)
    except TypeError as error:
        return f"Error: invalid arguments for {name}: {error}"
    except Exception as error:  # noqa: BLE001 - 工具错误要回传给模型，而不是打断循环
        return f"Error running {name}: {error}"


# ===== 来自 s01：响应读取辅助（保持） =====

def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ===== 来自 s01：核心循环；s02 修改工具执行入口 =====
# 与 lcc 只把 bash 替换为查表调用的写法一致；s02 额外保留 dispatch 注入，便于测试和复用。

def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., Any],
    dispatch: DispatchTool,
    system: str,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行支持多个工具的 Agent 循环。

    作用：在 s01 的基础上，把固定的 `run_bash(command)` 替换成通用的
    `dispatch(tool_name, arguments)`，让模型可以选择不同工具并传入各自参数。

    输入：
        messages：已有消息历史，会原地追加 assistant 和 tool_result 消息。
        create_message：创建模型响应的函数。
        dispatch：接收工具名和参数、返回工具结果文本的分发函数。
        system：系统提示词。
        tools：工具定义列表；未传入时使用本章的五个工具。
        max_tokens：单次模型响应的最大 token 数。

    输出：
        返回包含完整执行过程的消息历史。模型不再返回 `tool_use` 时结束。

    流程：模型响应 → 提取所有工具调用 → 按原始顺序分发 → 追加工具结果
    → 再次请求模型。核心 while 循环与 s01 相同，新增逻辑集中在分发器。
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
            name = _get(block, "name")
            arguments = _get(block, "input") or {}
            print(format_tool_call(name, arguments), flush=True)
            # s02 修改：相对 s01，工具执行从固定 bash 改为名称和参数分发。
            output = dispatch(name, arguments)
            print(format_tool_result(output), flush=True)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": results})


# ===== 来自 s01：终端输出辅助（保持） =====

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


# ===== 来自 s01：交互入口；s02 修改工具配置 =====

def main() -> None:
    """启动第二章的多工具终端 Agent。

    作用：加载模型配置，创建 Anthropic 客户端，把用户输入交给核心循环，
    并通过 `dispatch_tool` 统一处理模型选择的工具。模型请求配置与 s01
    保持一致，便于对比本章新增的工具分发部分。

    输入：用户在终端输入的自然语言任务。
    输出：工具执行摘要和模型最终回答；输入空行、`q` 或 `exit` 时退出。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("Set MODEL_ID in .env before running s02_tool_use.")

    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
    client_options: dict[str, Any] = {
        "timeout": timeout_seconds,
        "max_retries": max_retries,
    }
    if api_key := os.getenv("ANTHROPIC_API_KEY"):
        client_options["api_key"] = api_key
    if base_url := os.getenv("ANTHROPIC_BASE_URL"):
        client_options["base_url"] = base_url

    client = Anthropic(**client_options)
    # s02 修改：复用 s01 的顶层模型请求组件，入口逻辑只保留工具分发组装。
    requester = ModelRequester(client, model, timeout_seconds)

    print("s02：工具调用")
    print("输入任务，输入 q 退出。\n")

    history: list[Message] = []
    while True:
        try:
            query = input(format_user_prompt("s02")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        history.append({"role": "user", "content": query})
        try:
            # s02 修改：相对 s01，交互入口把多工具分发器注入核心循环。
            agent_loop(
                history,
                create_message=requester,
                dispatch=dispatch_tool,
                system=SYSTEM,
            )
        except RuntimeError as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print(format_assistant_message(_text_from_content(history[-1]["content"])))
        print()


if __name__ == "__main__":
    main()
