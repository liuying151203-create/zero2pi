#!/usr/bin/env python3
"""s03：工具权限策略。

本章在 s02 的工具分发前增加一层权限判断：

    tool_call -> permission check -> dispatch -> tool_result

权限策略属于 Agent Harness，而不是系统提示词。提示词只能影响模型的选择，
真正决定工具是否执行的代码必须在工具处理函数之前检查。
"""

from __future__ import annotations

import glob as glob_module
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_permission_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# ===== 来自 s02：消息、工具和模型调用类型 =====
# s03 保留 s02 的工具结构，只在分发前加入权限判断。

Message = dict[str, Any]
ToolHandler = Callable[..., str]
DispatchTool = Callable[[str, dict[str, Any]], str]
CreateMessage = Callable[..., Any]

WORKDIR = Path.cwd()

# ===== s03 修改：全局系统提示词 =====
# 只补充权限行为提示；权限判断仍由下面的 Python 代码负责。

SYSTEM = (
    f"你是运行在 {WORKDIR} 的编程 Agent。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。Windows 下使用 cmd.exe 命令。"
)


# ===== 来自 s02：工具处理函数（保持） =====

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


def safe_path(path: str) -> Path:
    """把相对路径解析到工作目录内，拒绝越界路径。"""
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


# ===== 来自 s02：工具定义和注册表（保持） =====

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

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ===== s03 新增：权限状态与判断结果 =====

class PermissionStatus(StrEnum):
    """工具调用经过权限检查后的三种状态。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    """描述权限状态及其面向用户的原因。"""

    status: PermissionStatus
    reason: str


PermissionCheck = Callable[[str, dict[str, Any]], PermissionDecision]
PermissionConfirm = Callable[[str, dict[str, Any], str], bool]

_READ_ONLY_TOOLS = {"read_file", "glob"}
_WRITE_TOOLS = {"write_file", "edit_file"}
_HARD_DENY_PATTERNS = (
    re.compile(r"\b(?:format|diskpart|mkfs)\b", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|restart)\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bdel\s+(?:/s\s+/q\s+)?[a-z]:\\(?:\*|$)", re.IGNORECASE),
)
_ASK_COMMAND_PATTERN = re.compile(
    r"\b(?:del|erase|rmdir|rd|move|ren|rename|powershell|remove-item|"
    r"set-content|out-file|curl|wget|invoke-webrequest)\b"
    r"|(?:>>?|&&|\|\|)",
    re.IGNORECASE,
)


def _bash_permission(command: str) -> PermissionDecision:
    """根据命令文本区分明显危险和需要确认的命令。"""
    if any(pattern.search(command) for pattern in _HARD_DENY_PATTERNS):
        return PermissionDecision(PermissionStatus.DENY, "命令属于禁止执行的高危操作")
    if _ASK_COMMAND_PATTERN.search(command):
        return PermissionDecision(PermissionStatus.ASK, "命令可能修改文件、访问网络或绕过专用工具")
    return PermissionDecision(PermissionStatus.ALLOW, "只读命令")


def check_permission(name: str, arguments: dict[str, Any]) -> PermissionDecision:
    """检查工具调用是否可以执行。

    作用：在 `dispatch_tool` 执行处理函数前建立统一的安全边界。

    输入：
        name：模型选择的工具名称。
        arguments：模型生成的工具参数。

    输出：
        `PermissionDecision`，分别表示自动允许、需要确认或直接拒绝。

    流程：先拒绝未知工具和越界路径，再按工具类型判断风险。只读工具自动允许，
    文件写入和编辑需要确认，bash 则根据命令规则进一步区分允许、询问和拒绝。
    这里是应用层策略，不替代操作系统沙箱；完整的工具生命周期拦截将在 s04 Hooks 实现。
    """
    if name not in TOOL_HANDLERS:
        return PermissionDecision(PermissionStatus.DENY, f"未知工具：{name}")

    if name in {"read_file", "write_file", "edit_file"}:
        try:
            safe_path(str(arguments.get("path", "")))
        except ValueError:
            return PermissionDecision(PermissionStatus.DENY, "路径超出工作区范围")

    if name in _READ_ONLY_TOOLS:
        return PermissionDecision(PermissionStatus.ALLOW, "只读工具")
    if name in _WRITE_TOOLS:
        return PermissionDecision(PermissionStatus.ASK, "工具将修改工作区文件")
    if name == "bash":
        return _bash_permission(str(arguments.get("command", "")))
    return PermissionDecision(PermissionStatus.DENY, "工具没有配置权限策略")


def confirm_permission(name: str, arguments: dict[str, Any], reason: str) -> bool:
    """在终端向用户询问是否允许一次工具调用。"""
    print(format_permission_request(name, arguments, reason), flush=True)
    try:
        answer = input("权限  是否允许？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


# ===== s03 修改：统一工具分发 =====
# 与 s02 的差异是先检查权限，再调用 handler；后续 s04 会把这一步抽成 before hook。

def dispatch_tool(name: str, arguments: dict[str, Any]) -> str:
    """根据工具名查找处理函数，并把模型参数传给它。"""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool: {name}"

    try:
        return handler(**arguments)
    except TypeError as error:
        return f"Error: invalid arguments for {name}: {error}"
    except Exception as error:  # noqa: BLE001 - 工具错误要回传给模型
        return f"Error running {name}: {error}"


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    dispatch: DispatchTool,
    permission: PermissionCheck = check_permission,
    confirm: PermissionConfirm = confirm_permission,
) -> str:
    """完成一次工具调用的权限检查、确认和执行。

    作用：把权限流程集中在一个入口，避免每个工具处理函数重复实现确认逻辑。
    输入：工具名称、工具参数、实际分发函数，以及可替换的权限检查和确认函数。
    输出：工具结果文本；拒绝或取消确认也会转成结果文本返回给模型。
    """
    decision = permission(name, arguments)
    if decision.status is PermissionStatus.DENY:
        return f"Permission denied: {decision.reason}"
    if decision.status is PermissionStatus.ASK and not confirm(name, arguments, decision.reason):
        return "Permission denied: user did not approve this tool call"
    return dispatch(name, arguments)


# ===== 来自 s02：响应读取辅助（保持） =====

def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


# ===== 来自 s02：核心循环；s03 修改工具执行入口 =====
# s02 直接 dispatch；s03 在这里统一经过 execute_tool，拒绝结果仍会回到模型。

def agent_loop(
    messages: list[Message],
    *,
    create_message: CreateMessage,
    dispatch: DispatchTool,
    system: str,
    permission: PermissionCheck = check_permission,
    confirm: PermissionConfirm = confirm_permission,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行带权限检查的多工具 Agent 循环。

    作用：在 s02 的工具调用循环中加入统一的权限门禁。
    输入：消息历史、模型请求函数、工具分发函数、系统提示词，以及可注入的权限函数。
    输出：包含 assistant 和 tool_result 消息的完整历史；模型不再请求工具时结束。
    流程：请求模型 → 提取工具调用 → 展示调用 → 权限检查/确认 → 执行或返回拒绝结果
    → 追加 tool_result → 再次请求模型。权限拒绝不会直接打断循环。
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
            output = execute_tool(
                name,
                arguments,
                dispatch=dispatch,
                permission=permission,
                confirm=confirm,
            )
            print(format_tool_result(output), flush=True)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": results})


# ===== 来自 s02：终端输出和交互入口；s03 修改权限配置 =====

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
    """启动第三章的权限控制 Agent。

    作用：加载模型配置，创建客户端，并把交互输入交给带权限门禁的核心循环。
    输入：用户在终端输入的自然语言任务。
    输出：工具调用、权限确认、工具结果和模型最终回答；空行、`q` 或 `exit` 退出。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("Set MODEL_ID in .env before running s03_permission.")

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

    def create_message(**kwargs: Any) -> Any:
        print(format_model_request(timeout_seconds), flush=True)
        try:
            return client.messages.create(model=model, **kwargs)
        except Exception as error:
            raise RuntimeError(f"模型请求失败：{error}") from error

    print("s03：工具权限")
    print("输入任务，输入 q 退出。\n")

    history: list[Message] = []
    while True:
        try:
            query = input(format_user_prompt("s03")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        history.append({"role": "user", "content": query})
        try:
            agent_loop(
                history,
                create_message=create_message,
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
