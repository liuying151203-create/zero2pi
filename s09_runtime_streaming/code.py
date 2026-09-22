#!/usr/bin/env python3
"""s09：运行时系统 · 流式响应。

s09 在 s08 的事件边界上增加文本增量输出：

    模型文本片段 -> assistant_delta -> TerminalEventSink
    模型最终消息 -> agent_loop -> 会话持久化与工具调用

增量只用于展示；最终完整消息仍是运行状态的唯一依据。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from anthropic import Anthropic
from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s08_runtime_events import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# s09 修改：流式章节使用独立会话目录，避免与 s08 的运行记录混用。
SESSION_ROOT = Path(".sessions/s09")

# ===== 来自 s08：Agent、工具与会话依赖（保持） =====
# s09 只改变模型响应和终端展示方式，工具、Hooks、压缩算法与会话格式保持不变。
Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextSummarizer = previous.ContextSummarizer
ContextCompactor = previous.ContextCompactor
SYSTEM = previous.SYSTEM
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
execute_tool = previous.execute_tool
make_permission_hook = previous.make_permission_hook
_positive_int_env = previous._positive_int_env

# ===== s09 修改：流式运行事件 =====

type AgentEventType = Literal[
    "model_request",
    "model_error",
    "assistant_delta",
    "assistant_message",
    "tool_call",
    "tool_result",
    "compaction",
    "agent_end",
]


# s09 修改：在 s08 完整事件的基础上增加文本增量和流式错误事件。
@dataclass(frozen=True)
class AgentEvent:
    """描述 Agent 运行过程中发生的一项事实。

    作用：统一承载完整运行事件和流式文本片段，使请求器与终端展示保持解耦。
    输入：事件类型，以及该事件对应的数据。
    输出：可交给任意 `EventSink` 的不可变事件对象。
    流程：请求器或核心循环创建事件 → EventSink 消费 → 终端或测试决定如何处理。
    """

    type: AgentEventType
    data: dict[str, Any]


type EventSink = Callable[[AgentEvent], None]


# s09 修改：增量文本需要记住当前回答是否已开始，因此终端消费者由函数变为小型状态对象。
@dataclass
class TerminalEventSink:
    """把完整事件和文本增量渲染到终端。

    作用：逐段显示模型文本，同时避免最终 `assistant_message` 再次打印同一份回答。
    输入：按发生顺序到达的 `AgentEvent`。
    输出：无；事件按既有终端样式写入标准输出。
    流程：首个 delta 打印前缀 → 后续 delta 直接追加 → 完整消息结束当前行；
    若没有收到 delta，则回退为一次性打印完整消息。
    """

    _streaming_answer: bool = False

    def __call__(self, event: AgentEvent) -> None:
        """消费一个事件，并维护流式回答的行状态。"""
        if event.type == "model_request":
            print(format_model_request(event.data["timeout_seconds"]), flush=True)
        elif event.type == "model_error":
            self._finish_streamed_answer()
        elif event.type == "assistant_delta":
            self._write_delta(str(event.data["text"]))
        elif event.type == "assistant_message":
            self._write_complete_message(event.data["message"])
        elif event.type == "tool_call":
            print(
                format_tool_call(event.data["name"], event.data["arguments"]),
                flush=True,
            )
        elif event.type == "tool_result":
            print(format_tool_result(event.data["output"]), flush=True)
        elif event.type == "compaction":
            source = (
                "模型摘要"
                if event.data["summary_kind"] == "model"
                else "确定性回退摘要"
            )
            print(
                "会话  已压缩 "
                f"{event.data['before_chars']} → {event.data['after_chars']} 字符，"
                f"总结 {event.data['summarized_messages']} 条、"
                f"保留 {event.data['retained_messages']} 条，使用{source}。",
                flush=True,
            )

    def _write_delta(self, text: str) -> None:
        """追加一个文本片段，并只在首个片段前打印 Agent 前缀。"""
        if not text:
            return
        if not self._streaming_answer:
            print("Agent  ", end="", flush=True)
            self._streaming_answer = True
        print(text.replace("\n", "\n       "), end="", flush=True)

    def _write_complete_message(self, message: Message) -> None:
        """结束增量输出；没有增量时一次性显示完整消息。"""
        if self._finish_streamed_answer():
            return
        text = s05._text_from_content(message["content"])
        if text:
            print(format_assistant_message(text), flush=True)

    def _finish_streamed_answer(self) -> bool:
        """结束正在输出的回答行，并返回此前是否存在增量文本。"""
        if not self._streaming_answer:
            return False
        print(flush=True)
        self._streaming_answer = False
        return True


# ===== s09 修改：阻塞摘要请求与流式回答请求 =====


# s09 修改：内部摘要继续使用完整响应，避免把压缩摘要作为用户可见文本流输出。
@dataclass(frozen=True)
class BlockingModelRequester:
    """发送不需要增量展示的内部模型请求。

    作用：供上下文摘要使用，保持 s08 的阻塞请求行为。
    输入：模型客户端、模型名称、超时时间、EventSink 和请求参数。
    输出：模型客户端返回的完整响应。
    流程：报告请求开始 → 等待完整响应 → 成功返回；失败时报告错误并抛出 RuntimeError。
    """

    client: Any
    model: str
    timeout_seconds: float
    emit: EventSink

    def __call__(self, **kwargs: Any) -> Any:
        """发送阻塞请求，不产生 `assistant_delta`。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={"timeout_seconds": self.timeout_seconds},
            )
        )
        try:
            return self.client.messages.create(model=self.model, **kwargs)
        except Exception as error:
            self.emit(AgentEvent(type="model_error", data={"message": str(error)}))
            raise RuntimeError(f"模型请求失败：{error}") from error


# s09 新增：正常 Agent 回答读取文本流，但最终仍返回 SDK 组装好的完整消息。
@dataclass(frozen=True)
class StreamingModelRequester:
    """流式请求模型并报告文本增量。

    作用：让用户立即看到文本生成过程，同时保持 agent_loop 的完整响应接口不变。
    输入：模型客户端、模型名称、超时时间、EventSink 和请求参数。
    输出：流结束后由 SDK 组装的完整模型响应，包含完整文本和工具调用。
    流程：报告请求开始 → 逐段发送 `assistant_delta` → 取得最终消息 → 返回；
    任一阶段失败时发送 `model_error`，再转换为 RuntimeError。
    """

    client: Any
    model: str
    timeout_seconds: float
    emit: EventSink

    def __call__(self, **kwargs: Any) -> Any:
        """消费模型文本流，并返回最终完整消息。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={"timeout_seconds": self.timeout_seconds},
            )
        )
        try:
            with self.client.messages.stream(model=self.model, **kwargs) as stream:
                for text in stream.text_stream:
                    if text:
                        self.emit(AgentEvent(type="assistant_delta", data={"text": text}))
                return stream.get_final_message()
        except Exception as error:
            # s09 新增：流中断时先让终端结束当前回答行，再由入口显示统一错误。
            self.emit(AgentEvent(type="model_error", data={"message": str(error)}))
            raise RuntimeError(f"模型请求失败：{error}") from error


# 来自 s08：保持；压缩和上下文投影行为不变，只改为发送 s09 事件类型。
@dataclass(frozen=True)
class CompactedContextRequester:
    """请求模型前按需压缩上下文，并向外报告压缩结果。

    作用：保持 s08 的压缩请求边界，把最新活跃上下文交给流式回答请求器。
    输入：底层请求函数、当前会话、压缩器、EventSink 和请求参数。
    输出：底层请求器返回的最终完整模型响应。
    流程：尝试压缩 → 报告压缩结果 → 重建活跃上下文 → 发起正常回答请求。
    """

    create_message: Callable[..., Any]
    session: SessionManager
    compactor: ContextCompactor
    emit: EventSink

    def __call__(self, **kwargs: Any) -> Any:
        """按需压缩，并使用最新会话上下文请求正常回答。"""
        outcome = self.compactor.compact_if_needed()
        if outcome is not None:
            self.emit(
                AgentEvent(
                    type="compaction",
                    data={
                        "summary_kind": outcome.summary_kind,
                        "before_chars": outcome.before_chars,
                        "after_chars": outcome.after_chars,
                        "summarized_messages": outcome.summarized_messages,
                        "retained_messages": outcome.retained_messages,
                    },
                )
            )
        kwargs["messages"] = self.session.build_context()
        return self.create_message(**kwargs)


# ===== 来自 s08：核心循环（保持并说明流式边界） =====
# 每个章节都展开完整 agent_loop；s09 的增量由请求器发出，循环仍只处理最终消息。


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
    emit: EventSink,
    save_message: SessionSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行支持文本增量展示的多工具 Agent 循环。

    作用：使用最终完整响应驱动消息持久化和工具循环，增量展示由请求器与 EventSink 完成。
    输入：当前消息、最终响应请求函数、工具分发、系统提示词、Hooks、EventSink 和保存函数。
    输出：包含本次执行过程的完整消息列表；模型不再调用工具时返回。
    流程：请求器流式展示并返回最终响应 → 保存完整 assistant 消息 → 执行完整 tool_use
    → 保存 tool_result → 继续请求；无工具调用时发出 agent_end。
    """
    while True:
        # s09 修改：调用期间可以产生多个 delta，但返回值始终是 SDK 组装的完整响应。
        response = create_message(
            system=system,
            messages=messages,
            tools=tools or TOOLS,
            max_tokens=max_tokens,
        )
        assistant_message = {
            "role": "assistant",
            "content": s05._to_jsonable(response.content),
        }
        messages.append(assistant_message)
        if save_message is not None:
            # 来自 s08：保持；只保存完整 assistant 消息，不持久化文本 delta。
            save_message(assistant_message)
        # s09 修改：完整事件结束终端流；已显示的增量不会被重复打印。
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        tool_calls = [
            block
            for block in assistant_message["content"]
            if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            # 来自 s08：保持；完整消息没有工具调用时结束本轮 Agent。
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            name = _get(block, "name")
            arguments = _get(block, "input") or {}
            # 来自 s08：保持；只有最终响应中的完整工具参数可以进入执行流程。
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"name": name, "arguments": arguments},
                )
            )
            output = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
            emit(
                AgentEvent(
                    type="tool_result",
                    data={"name": name, "output": output},
                )
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _get(block, "id"),
                    "content": output,
                }
            )

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)


# ===== 来自 s08：终端入口（修改） =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动使用文本增量事件展示回答的 Agent。

    作用：显式分离内部摘要请求和用户可见的流式回答请求，再组装 s09 运行时。
    输入：s05 保持的 --session 参数，以及终端中的自然语言任务。
    输出：模型文本逐段显示；完整消息、工具结果和压缩记录正常写入会话。
    流程：加载配置 → 创建终端消费者 → 组装阻塞摘要请求 → 组装流式回答请求
    → 注入压缩层和 agent_loop → 持续读取用户任务。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("请先在 .env 中设置 MODEL_ID，再运行 s09_runtime_streaming。")

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

    # s09 修改：有状态消费者统一处理增量文本和 s08 已有的完整运行事件。
    terminal = TerminalEventSink()

    # s09 新增：内部摘要不流式展示，正常回答才发送 assistant_delta。
    summary_requester = BlockingModelRequester(client, model, timeout_seconds, terminal)
    assistant_requester = StreamingModelRequester(client, model, timeout_seconds, terminal)

    # s09 修改：复用 s07/s08 会话格式与压缩算法，但使用 s09 专属默认目录。
    session_path = s05.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)
    policy = CompactionPolicy(
        max_context_chars=_positive_int_env("SESSION_COMPACTION_MAX_CHARS", 24000),
        keep_recent_chars=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_CHARS", 12000),
    )
    summary_max_tokens = _positive_int_env("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", 1024)
    summarizer = ContextSummarizer(
        create_message=summary_requester,
        max_tokens=summary_max_tokens,
    )
    compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
    request_with_context = CompactedContextRequester(
        create_message=assistant_requester,
        session=session,
        compactor=compactor,
        emit=terminal,
    )
    # 来自 s08：保持；Hooks 继续干预工具行为，流式事件只负责观察和展示。
    hooks = Hooks(before_tool_call=[make_permission_hook()])

    print("s09：运行时系统 · 流式响应")
    print(f"会话文件：{session.path}")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s09")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        # 来自 s08：保持；用户消息和最终模型消息继续完整持久化。
        session.append_message(user_message)
        active_context = session.build_context()
        try:
            agent_loop(
                active_context,
                create_message=request_with_context,
                dispatch=dispatch_tool,
                system=SYSTEM,
                hooks=hooks,
                emit=terminal,
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
            continue
        print()


if __name__ == "__main__":
    main()
