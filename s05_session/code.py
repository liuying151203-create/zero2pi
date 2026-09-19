#!/usr/bin/env python3
"""s05：线性会话持久化。

本章把 s04 的内存消息历史保存到 JSONL 文件：

    Message -> SessionMessage -> JsonlSessionStore -> SessionManager

先解决“退出后能够恢复对话”，暂不引入分支会话、上下文压缩和长期语义记忆。
"""

from __future__ import annotations

import json
import os
import re
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

# ===== 来自 s04：全局系统提示词（保持） =====
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
    """表示一条可以持久化的对话消息。

    属性：
        role：消息角色，例如 `user`、`assistant`。
        content：文本或结构化内容块，写入前会转换为 JSON 基础值。
        timestamp：消息写入时的 UTC 时间戳，用于观察记录顺序和调试。

    该模型只描述线性会话中的消息，不包含 Pi 后续会话树所需的 parentId、分支和事件类型。
    """

    role: str
    content: Any
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    @classmethod
    def from_message(cls, message: Message) -> SessionMessage:
        """从 Agent 内存消息创建可序列化的会话消息。

        输入：核心循环使用的消息字典。
        输出：带时间戳的 `SessionMessage`；内容中的 SDK 对象会递归转换为普通 JSON 值。
        """
        return cls(
            role=str(message["role"]),
            content=_to_jsonable(message.get("content", "")),
        )

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> SessionMessage:
        """从一条已解析的 JSONL 记录恢复会话消息。

        输入：包含 `role`、`content` 和可选 `timestamp` 的记录字典。
        输出：可供模型上下文使用的 `SessionMessage`；角色缺失或类型错误时抛出 `ValueError`。
        """
        role = record.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("Session message role must be a non-empty string")
        return cls(
            role=role,
            content=record.get("content", ""),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """把消息转换为 JSONL 存储层使用的记录字典。"""
        return {
            "type": "message",
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }

    def to_message(self) -> Message:
        """把持久化消息还原为模型 API 使用的消息字典。"""
        return {"role": self.role, "content": self.content}


# ===== s05 新增：JSONL 存储层 =====

class JsonlSessionStore:
    """负责会话 JSONL 文件的追加写入和顺序读取。

    该层只关心文件格式和 I/O，不决定会话如何参与 Agent loop，也不实现分支或检索。
    每次追加一行，读取时按文件顺序恢复消息；这种结构便于后续扩展为追加式事件日志。
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, message: SessionMessage) -> None:
        """追加一条消息记录。

        输入：已经完成模型内容标准化的 `SessionMessage`。
        输出：无；父目录不存在时创建目录，并向文件末尾写入一行 JSON。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(message.to_record(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line + "\n")

    def read_all(self) -> list[SessionMessage]:
        """按文件顺序读取全部消息，并在记录损坏时报告行号。

        输入：无，数据来源为构造函数指定的 JSONL 文件。
        输出：按写入顺序排列的消息列表；文件不存在时返回空列表。
        异常：JSON 无法解析、记录类型错误或消息角色非法时抛出带行号的 `ValueError`。
        """
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
    """协调消息模型与 JSONL 存储，向 Agent 提供加载和追加接口。

    作用：隔离 Agent loop 与具体文件格式，让核心循环只需要一个“保存消息”的函数，
    终端入口则通过管理器恢复历史并追加新消息。

    当前管理器表示一个活动的、线性的会话文件；多个会话的创建和选择由
    `SessionRepository` 负责，分支、父子节点和上下文压缩留到后续章节。
    """

    def __init__(self, store: JsonlSessionStore) -> None:
        self.store = store

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开一个已有或待创建的线性会话文件。

        输入：会话 JSONL 文件路径。
        输出：绑定到该路径的 `SessionManager`；文件不会在打开时立即创建。
        """
        return cls(JsonlSessionStore(path))

    @property
    def path(self) -> Path:
        """返回当前会话文件路径，供终端提示和调试使用。"""
        return self.store.path

    def load_messages(self) -> list[Message]:
        """读取会话并转换为模型消息历史。

        输出：按原始顺序排列、可直接传给模型客户端的消息字典列表。
        """
        return [entry.to_message() for entry in self.store.read_all()]

    def append_message(self, message: Message) -> None:
        """把一条 Agent 内存消息转换并追加到会话文件。

        输入：核心循环产生的 user、assistant 或工具结果消息。
        输出：无；转换和写入由 `SessionMessage` 与 `JsonlSessionStore` 完成。
        """
        self.store.append(SessionMessage.from_message(message))


# ===== s05 新增：会话目录管理 =====

class SessionRepository:
    """管理会话目录中的多个线性 JSONL 会话。

    作用：在 `SessionManager` 之上提供创建、列出和打开会话的能力，隔离终端命令
    与具体文件名规则。一个 Repository 对应一个会话目录，但同一时间只由入口选中
    一个当前 `SessionManager`。

    输入：会话目录路径。
    输出：会话路径列表，或指向指定会话的 `SessionManager`。
    边界：当前只支持独立的线性文件，不处理会话分支、树结构和跨文件合并。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    # s05 新增：列出当前会话目录中的独立 JSONL 文件。
    def list_sessions(self) -> list[Path]:
        """按文件名排序返回会话目录中的 JSONL 文件。"""
        if not self.root.exists():
            return []
        return sorted(path for path in self.root.glob("*.jsonl") if path.is_file())

    # s05 新增：创建空会话文件，并返回可继续追加消息的管理器。
    def create_session(self, name: str | None = None) -> SessionManager:
        """创建一个新的空会话并返回对应的管理器。

        输入：可选会话名；名称会被限制为当前目录下的安全文件名。
        输出：新建文件对应的 `SessionManager`。未提供名称时使用时间戳生成文件名，
        文件冲突时追加序号。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        stem = self._safe_stem(name) if name else datetime.now(UTC).strftime(
            "session-%Y%m%d-%H%M%S"
        )
        path = self.root / f"{stem}.jsonl"
        suffix = 2
        while path.exists():
            path = self.root / f"{stem}-{suffix}.jsonl"
            suffix += 1
        path.touch()
        return SessionManager.open(path)

    # s05 新增：把用户输入的序号或文件名解析为一个已有会话。
    def open_session(self, selector: str) -> SessionManager:
        """按序号或文件名打开一个已有会话。

        输入：`/sessions` 显示的 1-based 序号，或会话文件名/不带扩展名的文件名。
        输出：指向目标文件的 `SessionManager`。
        异常：选择器为空、包含目录穿越、序号不存在或文件不存在时抛出 `ValueError`。
        """
        selector = selector.strip()
        paths = self.list_sessions()
        if not selector:
            raise ValueError("请输入会话序号或文件名")
        if selector.isdigit():
            index = int(selector) - 1
            if index < 0 or index >= len(paths):
                raise ValueError(f"会话序号不存在：{selector}")
            return SessionManager.open(paths[index])

        candidate = Path(selector)
        if candidate.name != selector:
            raise ValueError("会话选择器不能包含目录路径")
        if candidate.suffix != ".jsonl":
            candidate = candidate.with_suffix(".jsonl")
        path = self.root / candidate.name
        if not path.is_file():
            raise ValueError(f"会话不存在：{selector}")
        return SessionManager.open(path)

    # s05 新增：统一清理自定义会话名，避免把目录路径当作文件名使用。
    @staticmethod
    def _safe_stem(name: str) -> str:
        """把用户输入的会话名转换为安全的文件名主体。"""
        if Path(name).name != name:
            raise ValueError("会话名不能包含目录路径")
        stem = re.sub(r"[^\w-]+", "-", Path(name).stem).strip("-_")
        if not stem:
            raise ValueError("会话名不能为空")
        return stem


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
    # s05 新增：通过可选保存函数接入会话，核心循环仍可只使用内存历史。
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


def show_sessions(repository: SessionRepository, current: SessionManager) -> None:
    """在终端显示会话序号、文件名、消息数量和当前标记。

    输入：会话目录管理器和当前活动会话。
    输出：无；直接打印可供 `/resume` 使用的会话列表。
    读取单个会话失败时只显示错误状态，不影响其他会话继续列出。
    """
    paths = repository.list_sessions()
    if not paths:
        print("暂无历史会话。")
        return

    current_path = current.path.resolve()
    for index, path in enumerate(paths, start=1):
        marker = "*" if path.resolve() == current_path else " "
        try:
            count = len(SessionManager.open(path).load_messages())
            detail = f"{count} 条消息"
        except ValueError as error:
            detail = f"读取失败：{error}"
        print(f"{marker} {index}. {path.name}（{detail}）")


def print_session_help() -> None:
    """打印 s05 支持的会话命令。"""
    print("/new [name]  创建新会话")
    print("/sessions    查看历史会话")
    print("/resume N    继续第 N 个会话")
    print("/help        查看命令帮助")
    print("/exit        退出程序")


# ===== s05 修改：终端入口接入会话管理 =====

def main() -> None:
    """启动带线性会话持久化的 Agent。

    作用：加载模型配置，打开会话文件，恢复历史消息，并把新消息持续写入 JSONL。
    输入：用户在终端输入的自然语言任务或会话命令；`SESSION_FILE` 可选地指定初始会话路径。
    输出：会话列表、恢复提示、工具调用、工具结果和模型回答；空行、`q` 或 `exit` 退出。
    会话命令：`/new` 创建、`/sessions` 查看、`/resume` 恢复、`/help` 帮助。
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

    # s05 新增：允许通过环境变量切换会话文件，默认使用项目内的运行时目录。
    session_path = Path(os.getenv("SESSION_FILE", ".sessions/default.jsonl"))
    # s05 新增：通过 SessionManager 隔离终端入口与 JSONL 存储实现。
    session = SessionManager.open(session_path)
    # s05 新增：会话仓库负责多个 JSONL 文件的创建、列出和切换。
    repository = SessionRepository(session.path.parent)
    # s05 新增：启动时恢复历史消息，后续请求会把它作为上下文发送给模型。
    history = session.load_messages()
    # 来自 s04：保持；s05 复用 s04 的权限 Hook，不在会话章节重复实现。
    hooks = Hooks(before_tool_call=[previous.make_permission_hook()])

    print("s05：会话持久化")
    print(f"会话文件：{session.path}")
    if history:
        print(f"已恢复 {len(history)} 条消息。")
    print("输入任务，输入 q 退出；输入 /help 查看会话命令。\n")

    while True:
        try:
            query = input(format_user_prompt("s05")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        # s05 新增：在自然语言任务之外解析会话管理命令。
        command, _, argument = query.partition(" ")
        if command == "/new":
            # s05 新增：创建会话并将当前内存历史切换为空列表。
            try:
                session = repository.create_session(argument or None)
                history = session.load_messages()
                print(f"已创建新会话：{session.path}\n")
            except ValueError as error:
                print(format_error(str(error)))
            continue
        if command == "/sessions":
            # s05 新增：展示会话序号，供 /resume 选择。
            show_sessions(repository, session)
            print()
            continue
        if command == "/resume":
            # s05 新增：打开目标会话并替换当前消息历史。
            try:
                session = repository.open_session(argument)
                history = session.load_messages()
                print(f"已切换会话：{session.path}（{len(history)} 条消息）\n")
            except ValueError as error:
                print(format_error(str(error)))
            continue
        if command == "/help":
            print_session_help()
            print()
            continue
        if command == "/exit":
            return
        if command.startswith("/"):
            print(format_error(f"未知命令：{command}，输入 /help 查看帮助"))
            continue

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
                # s05 修改：相对 s04，将 SessionManager 的追加方法注入核心循环。
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print(format_assistant_message(_text_from_content(history[-1]["content"])))
        print()


if __name__ == "__main__":
    main()
