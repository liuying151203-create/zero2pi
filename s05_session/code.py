#!/usr/bin/env python3
"""s05：线性会话持久化。

本章把 s04 的内存消息历史保存到 JSONL 文件：

    Message -> SessionMessage -> JsonlSessionStore -> SessionManager

先解决“退出后能够恢复对话”，暂不引入分支会话、上下文压缩和长期语义记忆。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from s04_hooks import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# ===== 来自 s04：工具、Hooks 和消息类型（复用） =====
# s05 只新增会话边界，不重复实现 s04 已验证的工具权限和生命周期逻辑。

Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
execute_tool = previous.execute_tool
WORKDIR = previous.WORKDIR

# ===== s05 修改：全局系统提示词 =====
# 会话是否持久化是 Harness 的内部行为，不需要写进系统提示词。

SYSTEM = (
    f"你是运行在 {WORKDIR} 的编程 Agent。"
    "只有任务需要操作工作区时才使用工具；问候和简单问题直接回答。"
    "优先使用最具体的工具，使用最少行动，完成后停止。"
)


# ===== s05 新增：消息模型 =====

def _to_jsonable(value: Any) -> Any:
    """把 SDK 对象递归转换为可写入 JSON 的基础值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return {
            str(key): _to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


@dataclass(frozen=True)
class SessionMessage:
    """表示一条可以持久化的对话消息。"""

    role: str
    content: Any
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    @classmethod
    def from_message(cls, message: Message) -> SessionMessage:
        """从 Agent 消息创建可序列化的会话消息。"""
        return cls(
            role=str(message["role"]),
            content=_to_jsonable(message.get("content", "")),
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> SessionMessage:
        """从 JSONL 记录恢复会话消息。"""
        role = record.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("Session message role must be a non-empty string")
        return cls(
            role=role,
            content=record.get("content", ""),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """转换为一条 JSONL 记录。"""
        return {
            "type": "message",
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }

    def to_message(self) -> Message:
        """转换为模型 API 使用的消息字典。"""
        return {"role": self.role, "content": self.content}


# ===== s05 新增：JSONL 存储层 =====

class JsonlSessionStore:
    """负责会话文件的追加写入和顺序读取。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, message: SessionMessage) -> None:
        """追加一条消息记录；父目录不存在时自动创建。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(message.to_record(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line + "\n")

    def read_all(self) -> list[SessionMessage]:
        """按文件顺序读取全部消息，并在记录损坏时报告行号。"""
        if not self.path.exists():
            return []

        messages: list[SessionMessage] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("type") != "message":
                    raise ValueError("record type must be message")
                messages.append(SessionMessage.from_record(record))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid session record at line {line_number}: {error}") from error
        return messages


# ===== s05 新增：会话管理层 =====

class SessionManager:
    """协调消息模型与 JSONL 存储，向 Agent 提供加载和追加接口。"""

    def __init__(self, store: JsonlSessionStore) -> None:
        self.store = store

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开一个已有或待创建的线性会话文件。"""
        return cls(JsonlSessionStore(path))

    @property
    def path(self) -> Path:
        """返回当前会话文件路径。"""
        return self.store.path

    def load_messages(self) -> list[Message]:
        """读取会话并转换为模型消息历史。"""
        return [entry.to_message() for entry in self.store.read_all()]

    def append_message(self, message: Message) -> None:
        """把一条 Agent 消息追加到会话文件。"""
        self.store.append(SessionMessage.from_message(message))


SessionSaver = Callable[[Message], None]


def _append_message(
    messages: list[Message],
    message: Message,
    save_message: SessionSaver | None,
) -> None:
    """同时更新内存历史和可选的持久化存储。"""
    messages.append(message)
    if save_message is not None:
        save_message(message)


# ===== s05 修改：核心循环接入会话保存 =====
# s04 通过 Hooks 扩展工具生命周期；s05 只在消息产生时追加持久化回调。

def _get(block: Any, name: str) -> Any:
    """兼容读取 SDK 对象和测试替身中的字段。"""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., Any],
    dispatch: DispatchTool,
    system: str,
    hooks: Hooks,
    save_message: SessionSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行带 Hooks 和会话保存能力的多工具 Agent 循环。

    作用：在 s04 的工具生命周期基础上，把每条 assistant 和 tool_result 消息
    写入可选的会话存储，使程序退出后能够恢复完整上下文。

    输入：消息历史、模型请求函数、工具分发函数、系统提示词、Hooks、可选的消息保存函数。
    输出：包含完整执行过程的消息历史；模型不再请求工具时结束。
    流程：请求模型 → 标准化并保存 assistant 消息 → 运行 Hooks 和工具 → 保存 tool_result
    → 再次请求模型。保存函数是可选的，因此测试和临时运行仍可只使用内存历史。
    """
    while True:
        response = create_message(
            system=system,
            messages=messages,
            tools=tools or TOOLS,
            max_tokens=max_tokens,
        )
        assistant_message = {
            "role": "assistant",
            "content": _to_jsonable(response.content),
        }
        # s05 修改：相对 s04，assistant 响应先标准化，再同时写入历史和会话文件。
        _append_message(messages, assistant_message, save_message)

        tool_calls = [
            block
            for block in assistant_message["content"]
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
                hooks=hooks,
            )
            print(format_tool_result(output), flush=True)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                }
            )

        tool_message = {"role": "user", "content": results}
        # s05 新增：工具结果也必须持久化，否则恢复后消息协议会不完整。
        _append_message(messages, tool_message, save_message)


# ===== 来自 s04：终端输出辅助（保持） =====

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


# ===== s05 修改：终端入口接入会话管理 =====

def main() -> None:
    """启动带线性会话持久化的 Agent。

    作用：加载模型配置，打开会话文件，恢复历史消息，并把新消息持续写入 JSONL。
    输入：用户在终端输入的自然语言任务；`SESSION_FILE` 可选地指定会话文件路径。
    输出：恢复提示、工具调用、工具结果和模型回答；空行、`q` 或 `exit` 退出。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("Set MODEL_ID in .env before running s05_session.")

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

    session_path = Path(os.getenv("SESSION_FILE", ".sessions/default.jsonl"))
    session = SessionManager.open(session_path)
    history = session.load_messages()
    hooks = Hooks(before_tool_call=[previous.make_permission_hook()])

    print("s05：会话持久化")
    print(f"会话文件：{session.path}")
    if history:
        print(f"已恢复 {len(history)} 条消息。")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s05")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        # s05 新增：用户消息在请求模型前落盘，保证中断后也能恢复输入。
        _append_message(history, user_message, session.append_message)
        try:
            agent_loop(
                history,
                create_message=create_message,
                dispatch=dispatch_tool,
                system=SYSTEM,
                hooks=hooks,
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print(format_assistant_message(_text_from_content(history[-1]["content"])))
        print()


if __name__ == "__main__":
    main()
