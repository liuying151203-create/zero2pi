#!/usr/bin/env python3
"""s11：运行时系统 · 追踪与用量。

s11 复用 s10 的 Provider 与运行事件，让同一份事件同时驱动三类互不干扰的消费者：

    AgentEvent -> TerminalEventSink
               -> JsonlTraceRecorder
               -> UsageTracker

终端展示、运行记录和指标统计不进入 Agent 决策，也不占用工具 Hook。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s10_model_provider import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# s11 修改：会话和 Trace 按章节隔离，便于分别检查模型上下文与运行事实。
SESSION_ROOT = Path(".sessions/s11")
TRACE_ROOT = Path(".traces/s11")

# ===== 来自 s10：Provider、Agent、工具与会话依赖（保持） =====
# s11 只观察既有运行过程，不改变工具、Hooks、压缩和会话格式。

Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextSummarizer = previous.ContextSummarizer
ContextCompactor = previous.ContextCompactor
ModelUsage = previous.ModelUsage
ModelResponse = previous.ModelResponse
ModelProvider = previous.ModelProvider
SYSTEM = previous.SYSTEM
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
execute_tool = previous.execute_tool
make_permission_hook = previous.make_permission_hook
create_model_provider = previous.create_model_provider
_positive_int_env = previous._positive_int_env

# ===== s11 修改：可观测运行事件 =====

type AgentEventType = Literal[
    "agent_start",
    "model_request",
    "model_response",
    "model_error",
    "assistant_delta",
    "assistant_message",
    "tool_call",
    "tool_result",
    "compaction",
    "agent_end",
]


# s11 修改：相对 s10 增加运行开始、模型完成和用量事件，供观察者统一消费。
@dataclass(frozen=True)
class AgentEvent:
    """描述 Agent 运行过程中已经发生的一项事实。

    作用：在运行组件与终端、Trace、指标统计之间建立只读事件边界。
    输入：稳定的事件类型，以及该事件对应的数据。
    输出：可发送给一个或多个 `EventSink` 的不可变事件对象。
    流程：运行组件创建事件 → EventDispatcher 广播 → 各观察者独立消费。
    """

    type: AgentEventType
    data: dict[str, Any]


type EventSink = Callable[[AgentEvent], None]


# s11 新增：一个事件可同时交给终端、Trace 和统计器，不再把观察逻辑塞进核心循环。
@dataclass
class EventDispatcher:
    """按注册顺序把同一个事件发送给多个观察者。

    作用：提供最小的事件多播能力，让新增观察者不需要修改 Agent loop。
    输入：实现 `EventSink` 签名的监听器列表，以及运行时发出的 `AgentEvent`。
    输出：无；每个监听器都会按注册顺序收到同一个事件。
    流程：注册监听器 → 核心组件调用 `emit()` → 依次通知监听器。

    当前实现保持同步和显式：监听器异常会直接暴露，避免教学阶段静默丢失 Trace。
    """

    listeners: list[EventSink] = field(default_factory=list)

    def subscribe(self, listener: EventSink) -> None:
        """注册一个事件监听器。"""
        self.listeners.append(listener)

    def emit(self, event: AgentEvent) -> None:
        """按注册顺序广播一个事件。"""
        for listener in tuple(self.listeners):
            listener(event)


# ===== 来自 s10：终端事件消费者（保持） =====


@dataclass
class TerminalEventSink:
    """把用户可见的运行事件渲染到终端。

    作用：保持 s10 的流式显示行为，并忽略只供 Trace 和统计使用的内部事件。
    输入：按发生顺序到达的 `AgentEvent`。
    输出：无；用户可见事件写入标准输出。
    流程：显示模型请求和文本增量 → 显示工具与压缩状态 → 完整消息结束流式行。
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
        """追加文本片段，并只在首个片段前打印 Agent 前缀。"""
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


# ===== s11 新增：运行统计 =====
# 来自 s10：保持；ModelUsage 已由 Provider 统一，s11 不再解析任何 SDK 字段。


@dataclass
class RunStats:
    """保存当前一次 Agent 运行累计得到的指标。"""

    model_requests: int = 0
    summary_requests: int = 0
    model_responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_duration_ms: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        """返回当前运行累计 Token 数。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


# s11 新增：统计器只观察事件，不修改消息、工具结果或 Agent 决策。
@dataclass
class UsageTracker:
    """从运行事件累计模型、工具和耗时指标。

    作用：为一次自然语言任务生成稳定的运行统计，不侵入 Agent loop 和工具 Hook。
    输入：EventDispatcher 按顺序发送的运行事件。
    输出：最新指标保存在 `stats`；`summary_text()` 返回终端摘要。
    流程：agent_start 重置 → 模型和工具事件累加 → agent_end 计算总耗时。
    """

    stats: RunStats = field(default_factory=RunStats)
    _started_at: float | None = None

    def __call__(self, event: AgentEvent) -> None:
        """消费一个事件并更新当前运行指标。"""
        if event.type == "agent_start":
            self.stats = RunStats()
            self._started_at = perf_counter()
        elif event.type == "model_request":
            self.stats.model_requests += 1
            if event.data["purpose"] == "summary":
                self.stats.summary_requests += 1
        elif event.type == "model_response":
            self.stats.model_responses += 1
            usage = event.data["usage"]
            self.stats.input_tokens += int(usage["input_tokens"])
            self.stats.output_tokens += int(usage["output_tokens"])
            self.stats.cache_read_tokens += int(usage["cache_read_tokens"])
            self.stats.cache_write_tokens += int(usage["cache_write_tokens"])
        elif event.type == "tool_call":
            self.stats.tool_calls += 1
        elif event.type == "tool_result":
            self.stats.tool_duration_ms += float(event.data["duration_ms"])
            if event.data["is_error"]:
                self.stats.tool_errors += 1
        elif event.type == "agent_end" and self._started_at is not None:
            self.stats.elapsed_ms = (perf_counter() - self._started_at) * 1000

    def summary_text(self) -> str:
        """返回一行便于阅读的当前运行摘要。"""
        return (
            f"运行  模型请求 {self.stats.model_requests} 次"
            f"（摘要 {self.stats.summary_requests} 次），"
            f"工具 {self.stats.tool_calls} 次，"
            f"Token {self.stats.total_tokens}，"
            f"耗时 {self.stats.elapsed_ms / 1000:.2f} 秒"
        )


# ===== s11 新增：紧凑 JSONL Trace =====


@dataclass(frozen=True)
class JsonlTraceRecorder:
    """把运行事件的紧凑投影追加到 JSONL 文件。

    作用：保存可复盘、可评测的运行事实，同时避免复制完整会话和大型工具结果。
    输入：Trace 文件路径和按顺序到达的 `AgentEvent`。
    输出：每个非增量事件写成一行 JSON；文本 delta 不单独落盘。
    流程：裁剪事件数据 → 添加 UTC 时间 → 追加 JSONL；完整消息仍由 Session 保存。
    """

    path: Path

    def __call__(self, event: AgentEvent) -> None:
        """把一个非增量事件写入 Trace。"""
        # s11 新增：逐 Token/文本片段落盘会制造大量重复数据，完整消息由会话保存。
        if event.type == "assistant_delta":
            return

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event.type,
            "data": _trace_event_data(event),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_trace_path(trace_root: Path = TRACE_ROOT) -> Path:
    """为当前进程生成一个不会覆盖旧记录的 Trace 路径。"""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    return trace_root / f"trace-{timestamp}.jsonl"


def _trace_event_data(event: AgentEvent) -> dict[str, Any]:
    """生成适合长期保存的紧凑事件数据。"""
    if event.type == "assistant_message":
        content = event.data["message"].get("content", [])
        blocks = content if isinstance(content, list) else []
        return {
            "content_types": [str(_get(block, "type")) for block in blocks],
            "text_chars": sum(
                len(str(_get(block, "text") or ""))
                for block in blocks
                if _get(block, "type") == "text"
            ),
        }
    if event.type == "tool_result":
        output = str(event.data["output"])
        return {
            "tool_call_id": event.data["tool_call_id"],
            "name": event.data["name"],
            "is_error": event.data["is_error"],
            "duration_ms": event.data["duration_ms"],
            "output_chars": len(output),
            "output_preview": output[:500],
        }
    if event.type == "agent_end":
        return {"message_count": len(event.data["messages"])}
    return s05._to_jsonable(event.data)


# ===== s11 修改：模型请求同时报告完成状态和 Token 用量 =====

type RequestPurpose = Literal["summary", "assistant"]


def _get(value: Any, name: str) -> Any:
    """兼容读取统一内容块、字典和测试替身中的字段。"""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


@dataclass(frozen=True)
class BlockingModelRequester:
    """发送内部阻塞请求，并报告请求结果和 Token 用量。

    作用：供上下文摘要使用，不产生用户可见文本增量，但参与完整用量统计。
    输入：s10 ModelProvider、超时时间、EventSink、请求用途和统一请求参数。
    输出：Provider 返回的 `ModelResponse`。
    流程：发送 model_request → provider.complete → 发送 model_response；失败时发送错误。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink
    purpose: RequestPurpose = "summary"

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """发送阻塞请求，并在完成时报告用量。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={
                    "model": self.provider.model,
                    "purpose": self.purpose,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
        )
        started_at = perf_counter()
        try:
            response = self.provider.complete(**kwargs)
        except Exception as error:
            duration_ms = (perf_counter() - started_at) * 1000
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：响应完成后一次性记录最终 usage，流式片段不重复计数。
        duration_ms = (perf_counter() - started_at) * 1000
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    "usage": response.usage.to_dict(),
                },
            )
        )
        return response


@dataclass(frozen=True)
class StreamingModelRequester:
    """流式请求正常回答，并在最终响应中报告 Token 用量。

    作用：保持 s10 的 Provider 流式体验，同时让统计器只消费一次最终 usage。
    输入：s10 ModelProvider、超时时间、EventSink、请求用途和统一请求参数。
    输出：Provider 组装完成的 `ModelResponse`。
    流程：发送 model_request → Provider 发送多个 assistant_delta → 取得最终响应
    → 发送一个 model_response；失败时发送 model_error。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink
    purpose: RequestPurpose = "assistant"

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """消费文本流，并在完成时报告一次最终响应。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={
                    "model": self.provider.model,
                    "purpose": self.purpose,
                    "timeout_seconds": self.timeout_seconds,
                },
            )
        )
        started_at = perf_counter()
        try:
            response = self.provider.stream(on_text=self._emit_text, **kwargs)
        except Exception as error:
            duration_ms = (perf_counter() - started_at) * 1000
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：以最终完整消息为唯一用量来源，避免按 delta 猜测 Token。
        duration_ms = (perf_counter() - started_at) * 1000
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    "usage": response.usage.to_dict(),
                },
            )
        )
        return response

    def _emit_text(self, text: str) -> None:
        """把 Provider 文本片段转换为流式展示事件。"""
        self.emit(AgentEvent(type="assistant_delta", data={"text": text}))


# 来自 s10：保持；压缩仍在正常回答请求前完成，只把结果发给 s11 事件分发器。
@dataclass(frozen=True)
class CompactedContextRequester:
    """请求模型前按需压缩上下文，并报告压缩结果。

    作用：保持 s10 的上下文边界，把最新活跃上下文交给流式请求器。
    输入：底层请求函数、当前会话、压缩器、EventSink 和请求参数。
    输出：底层请求器返回的最终完整响应。
    流程：尝试压缩 → 报告压缩 → 重建上下文 → 发起正常回答请求。
    """

    create_message: Callable[..., Any]
    session: SessionManager
    compactor: ContextCompactor
    emit: EventSink

    def __call__(self, **kwargs: Any) -> ModelResponse:
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


# ===== s11 修改：核心循环补充可度量的生命周期事件 =====
# 每个章节继续展开完整 agent_loop；s11 只在既有步骤附近补充 ID、耗时和错误状态。


def _tool_result_is_error(output: str) -> bool:
    """根据统一工具结果前缀判断本次执行是否失败。"""
    return output.startswith(("Error:", "Permission denied:", "Hook error:"))


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
    """运行可追踪、可统计的流式多工具 Agent 循环。

    作用：保持 s10 的 Provider—工具闭环，并为一次任务发出完整的运行事实。
    输入：消息、模型请求函数、工具分发、系统提示词、Hooks、EventSink 和保存函数。
    输出：包含本次执行过程的完整消息列表；模型不再调用工具时返回。
    流程：agent_start → 模型最终响应 → 保存 assistant → 工具调用及计时
    → 保存 tool_result → 继续请求；无工具调用时发送 agent_end。
    """
    # s11 新增：每次自然语言任务都有明确起点，统计器可在此重置本轮指标。
    emit(AgentEvent(type="agent_start", data={"message_count": len(messages)}))

    while True:
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
            # 来自 s10：保持；Trace 不替代会话，完整消息仍写入 Session。
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        tool_calls = [
            block
            for block in assistant_message["content"]
            if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            tool_call_id = str(_get(block, "id") or "")
            name = str(_get(block, "name") or "")
            arguments = _get(block, "input") or {}
            # s11 修改：工具 ID 进入事件，Trace 可以准确配对调用与结果。
            emit(
                AgentEvent(
                    type="tool_call",
                    data={
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "arguments": arguments,
                    },
                )
            )

            # s11 新增：只在工具执行边界计时，不让计时逻辑进入具体 handler。
            tool_started_at = perf_counter()
            output = execute_tool(name, arguments, dispatch=dispatch, hooks=hooks)
            tool_duration_ms = (perf_counter() - tool_started_at) * 1000
            emit(
                AgentEvent(
                    type="tool_result",
                    data={
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "output": output,
                        "is_error": _tool_result_is_error(output),
                        "duration_ms": tool_duration_ms,
                    },
                )
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": output,
                }
            )

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)


# ===== s11 修改：按观察层、模型层、上下文层和循环层显式组装 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动带 Trace 和用量统计的流式 Agent。

    作用：把终端、Trace 和 UsageTracker 注册为并列事件消费者，再组装既有运行时。
    输入：s05 保持的 `--session` 参数，以及终端中的自然语言任务。
    输出：流式回答、工具状态、每轮统计摘要、会话 JSONL 和紧凑 Trace JSONL。
    流程：加载配置 → 组装观察层 → 组装模型请求层 → 组装会话与压缩层
    → 注入 agent_loop → 每轮结束显示统计结果。
    """
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))

    # s11 修改：直接复用 s10 Provider，统计逻辑不再绑定 Anthropic SDK。
    provider = create_model_provider(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )

    # s11 新增：观察层横向组装；三个消费者接收同一事件，但职责互不重叠。
    terminal = TerminalEventSink()
    trace_path = new_trace_path()
    trace_recorder = JsonlTraceRecorder(trace_path)
    usage_tracker = UsageTracker()
    events = EventDispatcher()
    events.subscribe(terminal)
    events.subscribe(trace_recorder)
    events.subscribe(usage_tracker)

    # s11 修改：摘要和正常回答都向同一个分发器报告请求完成与 usage。
    summary_requester = BlockingModelRequester(
        provider,
        timeout_seconds,
        events.emit,
    )
    assistant_requester = StreamingModelRequester(
        provider,
        timeout_seconds,
        events.emit,
    )

    # 来自 s10：保持；会话与压缩仍是一条独立的数据组装链。
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
        emit=events.emit,
    )
    # 来自 s10：保持；Hook 干预行为，Event 只观察已经发生的事实。
    hooks = Hooks(before_tool_call=[make_permission_hook()])

    print("s11：运行时系统 · 追踪与用量")
    print(f"Provider：{os.getenv('MODEL_PROVIDER', 'anthropic')}")
    print(f"模型：{provider.model}")
    print(f"会话文件：{session.path}")
    print(f"Trace 文件：{trace_path}")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s11")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if query.lower() in {"", "q", "exit"}:
            return

        user_message = {"role": "user", "content": query}
        session.append_message(user_message)
        active_context = session.build_context()
        try:
            agent_loop(
                active_context,
                create_message=request_with_context,
                dispatch=dispatch_tool,
                system=SYSTEM,
                hooks=hooks,
                emit=events.emit,
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
            continue

        # s11 新增：统计结果来自 Event 消费者，不需要 agent_loop 返回额外对象。
        print(usage_tracker.summary_text())
        print()


if __name__ == "__main__":
    main()
