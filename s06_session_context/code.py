#!/usr/bin/env python3
"""s06：会话上下文投影。

本章将 s05 的“JSONL 文件等于模型 messages”改为 Pi 风格的两层结构：

    完整 MessageEntry 日志 -> build_session_context() -> 模型 messages

当前章节只保存和投影消息记录。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from s04_hooks import code as s04
from s05_session import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_user_prompt,
)

# s06 修改：使用独立目录，避免 s06 的嵌套 Record 与 s05 的扁平 Record 混在同一位置。
SESSION_ROOT = Path(".sessions/s06")

# ===== 来自 s05：Agent 运行时依赖（保持） =====
# s06 只改变会话日志到模型上下文的转换边界，工具、Hooks 和核心循环继续复用 s05。
Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SYSTEM = previous.SYSTEM
agent_loop = previous.agent_loop
dispatch_tool = previous.dispatch_tool
make_permission_hook = s04.make_permission_hook

# ===== s06 新增：术语约定 =====
# Message：模型 API 使用的 role/content 字典；MessageEntry：内存中的持久化消息对象；
# Record：MessageEntry 序列化为 JSONL 一行前后使用的原始 dict。MessageEntry 负责日志，
# Message 只表示模型可见上下文，二者不能再像 s05 一样视为同一列表。


# ===== s06 新增：追加式会话 Entry =====

def _normalize_message(message: Message) -> Message:
    """将消息标准化为可写入 JSON 的 role/content 字典。"""
    role = message.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("会话消息的 role 必须是非空字符串")
    return {
        "role": role,
        "content": previous._to_jsonable(message.get("content", "")),
    }


def _message_from_record(record: object) -> Message:
    """校验 JSON 记录中的消息字段并恢复为模型消息。"""
    if not isinstance(record, dict):
        raise TypeError("消息记录必须是对象")
    role = record.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("会话消息的 role 必须是非空字符串")
    return {"role": role, "content": record.get("content", "")}


@dataclass(frozen=True)
class MessageEntry:
    """记录一条原始 Agent 消息。

    作用：保存用户、助手和工具结果消息，作为会话的完整事实记录。
    输入：标准模型消息及可选时间戳。
    输出：可追加到 JSONL、也可直接投影为模型消息的 Entry。
    """

    # s06 新增：s05 的 SessionMessage 改为带 type 的 MessageEntry，供统一 Entry 日志使用。
    message: Message
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_message(cls, message: Message) -> MessageEntry:
        """从运行时消息创建可持久化 Entry。"""
        return cls(message=_normalize_message(message))

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> MessageEntry:
        """读取 s06 嵌套消息 Record。"""
        # s06 修改：章节目录隔离后不再兼容 s05 的扁平 Record，只接受自身格式。
        raw_message = record.get("message")
        return cls(
            message=_message_from_record(raw_message),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """转换为 s06 的嵌套消息 JSONL 记录。"""
        return {
            "type": "message",
            "message": self.message,
            "timestamp": self.timestamp,
        }


# s06 新增：在 JSONL 读取边界把无类型 Record 恢复为受约束的 MessageEntry 对象。
def entry_from_record(record: object) -> MessageEntry:
    """校验 message Record 并恢复为 MessageEntry。"""
    if not isinstance(record, dict):
        raise TypeError("会话记录必须是对象")
    if record.get("type") != "message":
        raise ValueError("会话记录类型必须是 message")
    return MessageEntry.from_record(record)


# ===== 来自 s05：JSONL 存储层（修改） =====
# s05 的 Store 读写 SessionMessage；s06 保持追加式 I/O，但改为读写 MessageEntry 日志。

class JsonlSessionStore:
    """以追加式 JSONL 保存和读取完整 MessageEntry 日志。

    作用：只处理消息日志的文件 I/O，不决定模型请求如何构建上下文。
    输入：会话 JSONL 文件路径。
    输出：按文件顺序读出的完整 `MessageEntry` 列表。
    流程：每次追加一行消息记录；读取时逐行解析，并在错误中保留行号。
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: MessageEntry) -> None:
        """将一个 MessageEntry 追加为一行 JSON。"""
        # s06 修改：参数由 SessionMessage 改为 MessageEntry，使日志对象与模型 Message 明确分层。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_record(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line + "\n")

    def read_all(self) -> list[MessageEntry]:
        """读取完整日志；损坏记录会以带行号的 ValueError 报告。"""
        if not self.path.exists():
            return []

        entries: list[MessageEntry] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                # s06 修改：读取 Record 后恢复为 MessageEntry，而不是直接返回模型 Message。
                entries.append(entry_from_record(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"无效会话记录，第 {line_number} 行：{error}") from error
        return entries


# s06 新增：将完整日志中的 MessageEntry 显式投影为模型 Message，作为唯一上下文入口。
def build_session_context(entries: Sequence[MessageEntry]) -> list[Message]:
    """把完整会话日志投影成单次模型请求所需的消息列表。

    作用：隔离“保存所有事实”和“模型看见哪些上下文”两个职责。
    输入：按写入顺序排列的完整 MessageEntry 日志。
    输出：按顺序排列、可直接传给模型的 Message 列表。
    流程：依次取出每个 Entry 内部的 Message，生成独立的模型上下文列表。
    """
    return [entry.message for entry in entries]


# ===== 来自 s05：SessionManager（修改） =====
# s05 的 Manager 直接返回完整 Message 历史；s06 先读取 MessageEntry 日志，再显式构建上下文。

class SessionManager:
    """协调 MessageEntry 日志、上下文投影和 Agent 的消息保存。

    作用：向运行时提供“追加完整事实”和“构建模型上下文”两套明确接口，
    防止 Agent loop 直接依赖 JSONL 文件格式。
    输入：绑定会话文件的 `JsonlSessionStore`。
    输出：完整日志、投影后的模型消息，或新追加的 MessageEntry。
    流程：读取完整 MessageEntry → 调用投影函数构建上下文；运行中把新消息追加为 MessageEntry。
    """

    def __init__(self, store: JsonlSessionStore) -> None:
        self.store = store

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开已有或待创建的会话 JSONL 文件。"""
        # 来自 s05：保持；调用方仍通过路径取得绑定会话文件的 Manager。
        return cls(JsonlSessionStore(path))

    @property
    def path(self) -> Path:
        """返回当前会话文件路径。"""
        # 来自 s05：保持；路径访问语义不随上下文投影改变。
        return self.store.path

    def load_entries(self) -> list[MessageEntry]:
        """读取未投影的完整会话日志。"""
        # s06 新增：替代 s05 的 load_messages()，让调用方可区分完整事实与模型上下文。
        return self.store.read_all()

    def build_context(self) -> list[Message]:
        """读取完整日志并构造当前模型上下文。"""
    # s06 新增：统一由投影规则产生模型输入，使 Session 与 Agent loop 保持隔离。
        return build_session_context(self.load_entries())

    def append_message(self, message: Message) -> None:
        """把 Agent 运行时消息作为完整事实追加到会话日志。"""
        # s06 修改：s05 直接追加 SessionMessage；现在包装成 MessageEntry 后再追加到统一日志。
        self.store.append(MessageEntry.from_message(message))


# ===== s06 新增：在模型请求边界注入上下文 =====

ContextBuilder = Callable[[], list[Message]]


def with_session_context(
    create_message: Callable[..., Any],
    build_context: ContextBuilder,
) -> Callable[..., Any]:
    """为既有模型请求函数注入最新的 Session 上下文。

    作用：保持 s05 `agent_loop()` 的工具循环不变，并在真正请求模型前统一投影会话。
    输入：底层模型请求函数，以及无参的上下文构建函数。
    输出：签名兼容 `create_message` 的包装函数。
    流程：接收 loop 参数 → 用最新 `build_context()` 覆盖 messages → 调用底层请求函数。
    """

    def request_with_context(**kwargs: Any) -> Any:
        # 参考 Pi：消息在请求边界实时投影，保存日志不再等同于模型输入。
        return create_message(**{**kwargs, "messages": build_context()})

    return request_with_context


# ===== 来自 s05：终端入口（修改） =====

def _text_from_content(content: Any) -> str:
    """提取模型响应中的文本块，用于终端展示。"""
    return previous._text_from_content(content)


def main(arguments: Sequence[str] | None = None) -> None:
    """启动使用 Session Entry 上下文投影的 Agent。

    作用：选择会话文件，追加完整 Entry 日志，并在每次模型请求前构建活跃上下文。
    输入：s05 保持的 `--session <路径>` 启动参数，以及终端中的自然语言任务。
    输出：会话文件提示、工具调用、工具结果和模型回答；空行、`q` 或 `exit` 退出。
    流程：打开会话 → 创建底层模型请求函数 → 注入 Session Context → 运行 s05 agent loop。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("请先在 .env 中设置 MODEL_ID，再运行 s06_session_context。")

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
        """调用配置好的模型客户端并展示请求状态。"""
        # 来自 s05：保持；底层 API 请求逻辑不关心消息来自完整历史还是投影结果。
        print(format_model_request(timeout_seconds), flush=True)
        try:
            return client.messages.create(model=model, **kwargs)
        except Exception as error:
            raise RuntimeError(f"模型请求失败：{error}") from error

    # s06 修改：复用 s05 的启动参数解析，但指定 s06 专属默认目录以隔离存储格式。
    session = SessionManager.open(
        previous.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    )
    # s06 修改：模型不直接加载全部记录，而是从 Entry 日志投影活跃上下文。
    initial_context = session.build_context()
    # s06 新增：以函数注入方式在每次请求前重新构建上下文，贴近 Pi 的投影边界。
    request_with_context = with_session_context(create_message, session.build_context)
    # 来自 s04：保持；会话投影不改变工具权限 Hook 的职责。
    hooks = Hooks(before_tool_call=[make_permission_hook()])

    print("s06：会话上下文投影")
    print(f"会话文件：{session.path}")
    print(f"初始模型上下文：{len(initial_context)} 条消息。")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s06")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        # s06 修改：完整用户输入先作为 MessageEntry 落盘，下一次请求由投影函数读取。
        session.append_message(user_message)
        # s06 修改：变量改为 active_context，明确它不是 s05 意义上的完整 history。
        active_context = session.build_context()
        try:
            agent_loop(
                active_context,
                create_message=request_with_context,
                dispatch=dispatch_tool,
                system=SYSTEM,
                hooks=hooks,
                # 来自 s05：保持；循环产生的助手和工具消息仍追加到完整会话日志。
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print(format_assistant_message(_text_from_content(active_context[-1]["content"])))
        print()


if __name__ == "__main__":
    main()
