#!/usr/bin/env python3
"""s08：运行时系统 · 事件边界。

本章把 Agent 的运行事实从终端展示中分离：

    模型请求 / 工具调用 / 上下文压缩 -> AgentEvent -> EventSink

核心循环只发出事件；终端函数决定如何显示，既有工具、Hooks 和会话行为保持不变。
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
from s07_session_compaction import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# s08 修改：使用独立会话目录，避免教学运行结果与 s07 混在一起。
SESSION_ROOT = Path(".sessions/s08")

# ===== 来自 s07：Agent 与会话依赖（保持） =====
# s08 不改变工具、Hooks 和压缩算法，只在它们的运行边界发出事件。
Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextSummarizer = previous.ContextSummarizer
ContextCompactor = previous.ContextCompactor
CompactionOutcome = previous.CompactionOutcome
SYSTEM = previous.SYSTEM
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
execute_tool = previous.execute_tool
make_permission_hook = previous.make_permission_hook
_positive_int_env = previous._positive_int_env

# ===== s08 新增：运行时事件 =====

type AgentEventType = Literal[
    "model_request",
    "assistant_message",
    "tool_call",
    "tool_result",
    "compaction",
    "agent_end",
]


# s08 新增：所有运行状态使用同一个简单事件结构向外报告。
@dataclass(frozen=True)
class AgentEvent:
    """描述 Agent 运行过程中发生的一项事实。

    作用：让核心循环报告运行状态，而不关心终端、日志或其他消费者如何处理。
    输入：稳定的事件类型，以及该事件需要携带的数据。
    输出：可交给任意 `EventSink` 的不可变事件对象。
    流程：运行组件创建事件 → 调用 `emit(event)` → 消费者读取类型和数据。
    """

    type: AgentEventType
    data: dict[str, Any]


type EventSink = Callable[[AgentEvent], None]


# s08 新增：终端只是一个事件消费者，不再由核心循环直接决定输出样式。
def print_event(event: AgentEvent) -> None:
    """把运行时事件显示为当前项目既有的终端样式。

    作用：集中处理模型、工具、压缩和最终回答的展示，不参与 Agent 决策。
    输入：核心运行组件发出的 `AgentEvent`。
    输出：无；需要展示的事件会写入标准输出。
    流程：按事件类型读取数据 → 调用对应格式化函数 → 输出到终端。
    """
    if event.type == "model_request":
        print(format_model_request(event.data["timeout_seconds"]), flush=True)
    elif event.type == "assistant_message":
        text = s05._text_from_content(event.data["message"]["content"])
        if text:
            print(format_assistant_message(text), flush=True)
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


# ===== s08 修改：事件化模型请求与压缩请求 =====


# s08 修改：相对公共 ModelRequester，不再直接打印，而是在请求前发出事件。
@dataclass(frozen=True)
class EventModelRequester:
    """发送模型请求，并在请求前报告 `model_request` 事件。

    作用：把模型调用状态交给事件消费者展示，同时统一包装客户端异常。
    输入：模型客户端、模型名称、超时时间、事件接收函数和本次请求参数。
    输出：模型客户端返回的响应对象。
    流程：发出请求事件 → 调用 Messages API → 成功返回响应，失败转换为 RuntimeError。
    """

    client: Any
    model: str
    timeout_seconds: float
    emit: EventSink

    def __call__(self, **kwargs: Any) -> Any:
        """报告请求状态并调用底层模型客户端。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={"timeout_seconds": self.timeout_seconds},
            )
        )
        try:
            return self.client.messages.create(model=self.model, **kwargs)
        except Exception as error:
            raise RuntimeError(f"模型请求失败：{error}") from error


# s08 修改：压缩结果改为事件，压缩和上下文投影行为仍复用 s07。
@dataclass(frozen=True)
class CompactedContextRequester:
    """请求模型前按需压缩上下文，并向外报告压缩结果。

    作用：保持 s07 的压缩请求边界，但通过 EventSink 输出压缩状态。
    输入：底层模型请求函数、当前会话、压缩器、事件接收函数和请求参数。
    输出：底层模型响应。
    流程：尝试压缩 → 发出压缩事件 → 重建活跃上下文 → 请求模型。
    """

    create_message: Callable[..., Any]
    session: SessionManager
    compactor: ContextCompactor
    emit: EventSink

    def __call__(self, **kwargs: Any) -> Any:
        """按需压缩并使用最新会话上下文请求模型。"""
        outcome = self.compactor.compact_if_needed()
        if outcome is not None:
            # s08 修改：请求包装器只报告事实，不再决定终端文案和样式。
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
        active_context = self.session.build_context()
        kwargs["messages"] = active_context
        return self.create_message(**kwargs)


# ===== 来自 s07：核心循环（修改） =====
# s08 完整保留循环结构，只把直接输出替换为运行时事件。


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
    """运行通过事件报告状态的多工具 Agent 循环。

    作用：完整执行模型与工具循环，同时把助手消息、工具调用和结束状态交给外部消费者。
    输入：当前消息、模型请求函数、工具分发、系统提示词、Hooks、EventSink 和保存函数。
    输出：包含本次执行过程的消息列表；模型不再调用工具时返回。
    流程：请求模型 → 保存并发出 assistant 事件 → 发出工具调用事件 → 执行工具
    → 发出工具结果事件并保存 → 无工具调用时发出 agent_end。
    """
    while True:
        # 来自 s07：保持；请求组件仍会在真正模型调用前检查上下文压缩。
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
            save_message(assistant_message)
        # s08 修改：助手消息由循环报告，最终是否展示及如何展示由 EventSink 决定。
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        tool_calls = [
            block
            for block in assistant_message["content"]
            if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            # s08 新增：显式报告本轮 Agent 已结束，消费者无需推测循环退出位置。
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            name = _get(block, "name")
            arguments = _get(block, "input") or {}
            # s08 修改：工具调用先发事件，Hooks 和工具执行顺序保持 s07 不变。
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"name": name, "arguments": arguments},
                )
            )
            output = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
            # s08 修改：事件携带完整结果；终端消费者可以只显示截断预览。
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


# ===== 来自 s07：终端入口（修改） =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动通过事件连接运行时与终端展示的 Agent。

    作用：创建 s08 会话及压缩组件，并把同一个终端 EventSink 注入各运行组件。
    输入：s05 保持的 --session 参数，以及终端中的自然语言任务。
    输出：由 `print_event()` 统一显示模型、压缩、工具和助手事件；空行、q 或 exit 退出。
    流程：加载配置 → 组装事件化请求与压缩组件 → 运行 Agent loop → 消费运行时事件。
    """
    load_dotenv(override=True)
    model = os.getenv("MODEL_ID")
    if not model:
        raise RuntimeError("请先在 .env 中设置 MODEL_ID，再运行 s08_runtime_events。")

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

    # s08 新增：同一个 EventSink 同时接收模型、压缩和核心循环事件。
    emit = print_event
    requester = EventModelRequester(client, model, timeout_seconds, emit)

    # s08 修改：复用 s07 会话格式和压缩算法，但使用 s08 专属默认目录。
    session_path = s05.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)
    policy = CompactionPolicy(
        max_context_chars=_positive_int_env("SESSION_COMPACTION_MAX_CHARS", 24000),
        keep_recent_chars=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_CHARS", 12000),
    )
    summary_max_tokens = _positive_int_env("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", 1024)
    summarizer = ContextSummarizer(
        create_message=requester,
        max_tokens=summary_max_tokens,
    )
    compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
    request_with_context = CompactedContextRequester(
        create_message=requester,
        session=session,
        compactor=compactor,
        emit=emit,
    )
    # 来自 s07：保持；Hooks 仍负责干预行为，EventSink 只负责观察。
    hooks = Hooks(before_tool_call=[make_permission_hook()])

    print("s08：运行时系统 · 事件边界")
    print(f"会话文件：{session.path}")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s08")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        # 来自 s07：保持；事件只改变运行状态的传递方式，不改变完整会话持久化。
        session.append_message(user_message)
        active_context = session.build_context()
        try:
            agent_loop(
                active_context,
                create_message=request_with_context,
                dispatch=dispatch_tool,
                system=SYSTEM,
                hooks=hooks,
                emit=emit,
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(f"\n{format_error(str(error))}", file=sys.stderr)
            continue
        print()


if __name__ == "__main__":
    main()
