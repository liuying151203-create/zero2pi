#!/usr/bin/env python3
"""s11：运行时系统 · 追踪与用量。

s11 复用 s10 的 Provider 与运行事件；运行事件驱动终端、Trace 和本轮统计，
会话记录保存回答与摘要各自的用量：

    AgentEvent -> TerminalEventSink
               -> JsonlTraceRecorder
               -> UsageTracker

终端展示和运行统计不进入 Agent 决策，也不占用工具 Hook。
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
from s07_session_compaction import code as s07
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
# s11 保留工具与 Hooks；会话 Entry 在下方增加用量字段。

Message = previous.Message
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
CompactionPolicy = previous.CompactionPolicy
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

# ===== s11 新增：会话中的模型用量 =====


def _usage_from_record(value: object) -> ModelUsage | None:
    """把可选的 JSON 用量还原为统一模型用量。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("usage 必须是对象或 null")
    names = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    counts = {}
    for name in names:
        # s11 修正：部分字段缺失不能被补成零，否则历史统计会低估。
        if name not in value:
            raise ValueError(f"usage 缺少 {name}")
        count = value[name]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"usage.{name} 必须是非负整数")
        counts[name] = count
    return ModelUsage(**counts)


def _known_usage(usage: ModelUsage | None) -> ModelUsage | None:
    """把 s10 的全零占位视为未报告用量。

    s10 的 `ModelResponse` 固定携带 `ModelUsage`，Provider 缺少 usage 时会生成全零对象。
    实际模型调用至少消耗输入或输出 Token，因此 s11 将全零解释为未知。
    """
    if usage is None or usage.total_tokens == 0:
        return None
    return usage


# s11 新增：参考 Pi，把 assistant 的用量和产生它的消息保存在同一条 Entry。
@dataclass(frozen=True)
class MessageEntry(s07.MessageEntry):
    """保存模型消息；assistant 消息还携带模型、停止原因和可选用量。"""

    model: str | None = None
    stop_reason: str | None = None
    usage: ModelUsage | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> MessageEntry:
        """读取本章或旧版 s11 的消息记录。"""
        base = s07.MessageEntry.from_record(record)
        return cls(
            message=base.message,
            timestamp=base.timestamp,
            model=record.get("model"),
            stop_reason=record.get("stop_reason"),
            usage=_usage_from_record(record.get("usage")),
        )

    def to_record(self) -> dict[str, Any]:
        """将消息及其可选模型元数据写入同一条记录。"""
        record = super().to_record()
        if self.model is not None:
            record.update(
                model=self.model,
                stop_reason=self.stop_reason,
                usage=self.usage.to_dict() if self.usage is not None else None,
            )
        return record


# s11 新增：参考 Pi，CompactionEntry 只保存真正生成摘要的那次用量。
@dataclass(frozen=True)
class CompactionEntry(s07.CompactionEntry):
    """保存压缩结果，以及生成摘要的模型和可选用量。"""

    summary_model: str | None = None
    summary_usage: ModelUsage | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> CompactionEntry:
        """读取本章或旧版 s11 的压缩记录。"""
        base = s07.CompactionEntry.from_record(record)
        return cls(
            summary=base.summary,
            retained_tail=base.retained_tail,
            timestamp=base.timestamp,
            summary_model=record.get("summary_model"),
            summary_usage=_usage_from_record(record.get("summary_usage")),
        )

    def to_record(self) -> dict[str, Any]:
        """把摘要请求的用量附在压缩记录上。"""
        record = super().to_record()
        record["summary_model"] = self.summary_model
        record["summary_usage"] = (
            self.summary_usage.to_dict() if self.summary_usage is not None else None
        )
        return record


# s11 新增：抛异常时没有 assistant/compaction，可用独立记录保留已知用量。
@dataclass(frozen=True)
class ModelErrorEntry:
    """保存没有形成模型消息的一次失败调用及其可选用量。"""

    model: str
    purpose: str
    usage: ModelUsage | None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ModelErrorEntry:
        """读取失败调用记录。"""
        return cls(
            model=str(record["model"]),
            purpose=str(record["purpose"]),
            usage=_usage_from_record(record.get("usage")),
            timestamp=str(record.get("timestamp", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """把失败调用转换为 JSONL 记录。"""
        return {
            "type": "model_error",
            "model": self.model,
            "purpose": self.purpose,
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "timestamp": self.timestamp,
        }


type SessionEntry = MessageEntry | CompactionEntry | ModelErrorEntry
type AssistantSaver = Callable[[Message, ModelResponse], None]


def entry_from_record(record: object) -> SessionEntry:
    """根据类型恢复本章实际使用的三种会话 Entry。"""
    if not isinstance(record, dict):
        raise TypeError("会话记录必须是对象")
    if record.get("type") == "message":
        return MessageEntry.from_record(record)
    if record.get("type") == "compaction":
        return CompactionEntry.from_record(record)
    if record.get("type") == "model_error":
        return ModelErrorEntry.from_record(record)
    raise ValueError("会话记录类型必须是 message、compaction 或 model_error")


# s11 修改：读取时识别用量元数据，写入仍复用 s07 的追加式 JSONL。
class JsonlSessionStore(s07.JsonlSessionStore):
    """读取带用量的 s11 会话记录，并保留原有追加式存储。"""

    def read_all(self) -> list[SessionEntry]:
        """逐行读取消息、压缩和失败调用记录。"""
        if not self.path.exists():
            return []
        entries: list[SessionEntry] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                entries.append(entry_from_record(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"无效会话记录，第 {line_number} 行：{error}") from error
        return entries


# s11 修改：保存接口显式区分普通消息、模型回答、压缩和失败调用。
class SessionManager(s07.SessionManager):
    """管理 s11 会话的完整记录，并向运行时提供不含统计元数据的模型上下文。

    输入：绑定会话文件的存储对象，以及按调用结果产生的消息、摘要或异常。
    输出：可重放的会话 Entry、模型上下文和会话累计用量。
    流程：按类型追加记录；加载时还原元数据；构建上下文时沿用 s07 投影。
    """

    @classmethod
    def open(cls, path: Path) -> SessionManager:
        """打开一个 s11 会话文件。"""
        return cls(JsonlSessionStore(path))

    def append_assistant(self, message: Message, response: ModelResponse) -> None:
        """把 assistant 内容与生成它的模型用量保存为同一条记录。"""
        base = MessageEntry.from_message(message)
        self.store.append(
            MessageEntry(
                message=base.message,
                model=response.model,
                stop_reason=response.stop_reason,
                usage=_known_usage(response.usage),
            )
        )

    def append_compaction(
        self,
        summary: str,
        retained_tail: Sequence[Message],
        summary_model: str | None = None,
        summary_usage: ModelUsage | None = None,
    ) -> None:
        """把摘要、保留尾部和生成摘要的那次用量一起保存。"""
        base = CompactionEntry.create(summary, retained_tail)
        self.store.append(
            CompactionEntry(
                summary=base.summary,
                retained_tail=base.retained_tail,
                summary_model=summary_model,
                summary_usage=_known_usage(summary_usage),
            )
        )

    def append_model_error(self, model: str, purpose: str, usage: ModelUsage | None) -> None:
        """保存没有形成消息的失败模型调用。"""
        self.store.append(ModelErrorEntry(model=model, purpose=purpose, usage=usage))

    def session_stats(self) -> SessionStats:
        """从完整会话历史重算累计用量，包含已压缩的旧消息。"""
        return SessionStats.from_entries(self.load_entries())


# s11 新增：会话累计只从持久化 Entry 重算，压缩后的旧调用不会消失。
@dataclass
class SessionStats:
    """从持久化会话记录计算累计模型调用和已知 Token 用量。

    输入：全部消息、压缩和失败调用 Entry。
    输出：请求总数、失败数、未知用量次数及已知 Token 总数。
    流程：逐条读取对应调用的 usage；压缩不删除旧记录，恢复后可重新汇总。
    """

    model_requests: int = 0
    summary_requests: int = 0
    failed_requests: int = 0
    tool_calls: int = 0
    unknown_usage_requests: int = 0
    legacy_entries: int = 0
    known_tokens: int = 0

    @classmethod
    def from_entries(cls, entries: Sequence[SessionEntry]) -> SessionStats:
        """按记录归属累计回答、摘要和无消息失败调用。"""
        stats = cls()
        # s11 新增：一条调用只在其归属的 Entry 计数一次。
        for entry in entries:
            if isinstance(entry, MessageEntry) and entry.message["role"] == "assistant":
                # s11 新增：参考 Pi，从已保存的 assistant 内容块重算工具调用数。
                content = entry.message["content"]
                if isinstance(content, list):
                    stats.tool_calls += sum(_get(block, "type") == "tool_use" for block in content)
            if isinstance(entry, ModelErrorEntry):
                stats.model_requests += 1
                stats.failed_requests += 1
                if entry.purpose == "summary":
                    stats.summary_requests += 1
                stats.add_usage(entry.usage)
            elif isinstance(entry, CompactionEntry):
                if entry.summary_model is not None:
                    stats.model_requests += 1
                    stats.summary_requests += 1
                    stats.add_usage(entry.summary_usage)
            elif isinstance(entry, MessageEntry) and entry.model is not None:
                stats.model_requests += 1
                if entry.stop_reason in {"error", "aborted"}:
                    stats.failed_requests += 1
                stats.add_usage(entry.usage)
            elif isinstance(entry, MessageEntry) and entry.message["role"] == "assistant":
                # s11 新增：旧版 assistant 记录缺少模型元数据，标明历史统计不完整。
                stats.legacy_entries += 1
        return stats

    def add_usage(self, usage: ModelUsage | None) -> None:
        """只累加已知用量；缺失值单独计数。"""
        if usage is None:
            self.unknown_usage_requests += 1
        else:
            self.known_tokens += usage.total_tokens

    def summary_text(self) -> str:
        """返回可直接展示的会话累计摘要。"""
        suffix = (
            f"，{self.unknown_usage_requests} 次用量未知" if self.unknown_usage_requests else ""
        )
        legacy = f"；旧记录 {self.legacy_entries} 条未统计" if self.legacy_entries else ""
        return (
            f"会话  模型请求 {self.model_requests} 次"
            f"（摘要 {self.summary_requests} 次，失败 {self.failed_requests} 次），"
            f"工具 {self.tool_calls} 次，"
            f"已知 Token {self.known_tokens}{suffix}{legacy}"
        )


# s11 新增：只有未形成消息的异常调用走事件落盘，防止与 assistant 重复计费。
@dataclass(frozen=True)
class SessionErrorRecorder:
    """在模型请求抛异常时，将未形成消息的调用立即写入当前会话。"""

    session: SessionManager

    def __call__(self, event: AgentEvent) -> None:
        """仅消费 model_error，其他事件由各自观察者处理。"""
        if event.type == "model_error":
            self.session.append_model_error(
                model=event.data["model"],
                purpose=event.data["purpose"],
                usage=_usage_from_record(event.data.get("usage")),
            )


# ===== s11 修改：可观测运行事件 =====

type AgentEventType = Literal[
    "agent_start",
    "model_request",
    "model_response",
    "model_error",
    "summary_empty",
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
            source = "模型摘要" if event.data["summary_kind"] == "model" else "确定性回退摘要"
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
    failed_requests: int = 0
    unknown_usage_requests: int = 0
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
            # s11 修改：模型返回错误状态也计入用量；缺失 usage 单独计数。
            if event.data.get("stop_reason") in {"error", "aborted"}:
                self.stats.failed_requests += 1
            self._add_usage(event.data.get("usage"))
        elif event.type == "model_error":
            # s11 新增：抛异常的请求可能已经产生可获知的 Token 用量。
            self.stats.failed_requests += 1
            self._add_usage(event.data.get("usage"))
        elif event.type == "summary_empty":
            # s11 新增：模型虽返回但未生成可用摘要，只增加失败次数，不重复计费。
            self.stats.failed_requests += 1
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
        # s11 修正：模型异常会跳过 agent_end，仍显示从本轮开始到现在的耗时。
        elapsed_ms = self.stats.elapsed_ms
        if elapsed_ms == 0 and self._started_at is not None:
            elapsed_ms = (perf_counter() - self._started_at) * 1000
        unknown = (
            f"，{self.stats.unknown_usage_requests} 次用量未知"
            if self.stats.unknown_usage_requests
            else ""
        )
        return (
            f"运行  模型请求 {self.stats.model_requests} 次"
            f"（摘要 {self.stats.summary_requests} 次，失败 {self.stats.failed_requests} 次），"
            f"工具 {self.stats.tool_calls} 次，"
            f"已知 Token {self.stats.total_tokens}{unknown}，"
            f"耗时 {elapsed_ms / 1000:.2f} 秒"
        )

    def _add_usage(self, usage: dict[str, Any] | None) -> None:
        """累计已知用量，并区分完全缺失的 usage。"""
        if usage is None:
            self.stats.unknown_usage_requests += 1
            return
        self.stats.input_tokens += int(usage["input_tokens"])
        self.stats.output_tokens += int(usage["output_tokens"])
        self.stats.cache_read_tokens += int(usage["cache_read_tokens"])
        self.stats.cache_write_tokens += int(usage["cache_write_tokens"])


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


def _usage_from_error(error: Exception) -> ModelUsage | None:
    """读取异常或其响应里实际可用的 Token 用量；缺失时返回未知。"""
    raw = _get(error, "usage")
    if raw is None:
        raw = _get(_get(error, "response"), "usage")
    if isinstance(raw, ModelUsage):
        return _known_usage(raw)
    if raw is None:
        return None
    if _get(raw, "input_tokens") is not None and _get(raw, "output_tokens") is not None:
        return _known_usage(
            ModelUsage(
                input_tokens=int(_get(raw, "input_tokens") or 0),
                output_tokens=int(_get(raw, "output_tokens") or 0),
                cache_read_tokens=int(_get(raw, "cache_read_tokens") or 0),
                cache_write_tokens=int(_get(raw, "cache_write_tokens") or 0),
            )
        )
    if _get(raw, "prompt_tokens") is not None:
        return _known_usage(previous._openai_usage(raw))
    return None


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
            # s11 修改：异常若携带 usage，同一次失败调用仍计入用量。
            error_usage = _usage_from_error(error)
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                        "usage": error_usage.to_dict() if error_usage is not None else None,
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：响应完成后一次性记录最终 usage，流式片段不重复计数。
        duration_ms = (perf_counter() - started_at) * 1000
        # s11 修改：把 s10 的全零占位统一解释为未知用量。
        known_usage = _known_usage(response.usage)
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    # s11 修改：Provider 未报告用量时保持 null，而不是伪造零 Token。
                    "usage": known_usage.to_dict() if known_usage is not None else None,
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
            # s11 修改：流式请求失败也可能返回已消耗的 usage。
            error_usage = _usage_from_error(error)
            self.emit(
                AgentEvent(
                    type="model_error",
                    data={
                        "model": self.provider.model,
                        "purpose": self.purpose,
                        "duration_ms": duration_ms,
                        "message": str(error),
                        "usage": error_usage.to_dict() if error_usage is not None else None,
                    },
                )
            )
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

        # s11 新增：以最终完整消息为唯一用量来源，避免按 delta 猜测 Token。
        duration_ms = (perf_counter() - started_at) * 1000
        # s11 修改：Provider 缺失 usage 时，不把 s10 全零占位当成实际消耗。
        known_usage = _known_usage(response.usage)
        self.emit(
            AgentEvent(
                type="model_response",
                data={
                    "model": response.model,
                    "purpose": self.purpose,
                    "stop_reason": response.stop_reason,
                    "duration_ms": duration_ms,
                    # s11 修改：成功结束但没有 usage 时仍保留未知状态。
                    "usage": known_usage.to_dict() if known_usage is not None else None,
                },
            )
        )
        return response

    def _emit_text(self, text: str) -> None:
        """把 Provider 文本片段转换为流式展示事件。"""
        self.emit(AgentEvent(type="assistant_delta", data={"text": text}))


# ===== s11 修改：摘要结果携带用量，压缩后随 CompactionEntry 保存 =====


# s11 新增：摘要器向压缩器明确传递摘要、模型及用量。
@dataclass(frozen=True)
class SummaryResult:
    """保存可用摘要及其对应模型响应的可选用量。"""

    text: str | None
    model: str | None
    usage: ModelUsage | None


# s11 修改：相对 s07，空摘要立即记录为无可用摘要的调用。
@dataclass(frozen=True)
class ContextSummarizer:
    """请求模型更新会话摘要，并保存没有生成摘要的调用。

    输入：待总结消息、上一份摘要、摘要请求器、会话和输出上限。
    输出：可用摘要及其模型与用量；请求均无文本时返回空结果。
    流程：序列化消息 → 请求摘要 → 无文本时记录失败调用 → 必要时重试。
    """

    create_message: Callable[..., ModelResponse]
    max_tokens: int
    session: SessionManager
    emit: EventSink

    def __call__(
        self,
        messages_to_summarize: Sequence[Message],
        previous_summary: str | None,
    ) -> SummaryResult:
        """按 s07 的提示词生成摘要，空响应的用量立即进入失败记录。"""
        transcript = s07.serialize_context_for_summary(messages_to_summarize)
        previous_section = (
            f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            if previous_summary
            else ""
        )
        prompt = (
            f"{previous_section}<unsummarized-messages>\n{transcript}\n"
            "</unsummarized-messages>\n\n"
            "请生成更新后的完整摘要。"
        )
        token_limits = [self.max_tokens]
        if self.max_tokens < 1024:
            token_limits.append(1024)
        for token_limit in token_limits:
            response = self.create_message(
                system=s07.COMPACTION_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=token_limit,
            )
            summary = s05._text_from_content(response.content).strip()
            # s11 修改：错误/中断响应即使带有部分文本，也不能作为可信摘要。
            if summary and response.stop_reason not in {"error", "aborted"}:
                return SummaryResult(summary, response.model, _known_usage(response.usage))
            # s11 新增：空摘要没有对应的压缩结果，立即保存其已知或未知用量。
            self.session.append_model_error(
                response.model,
                "summary",
                _known_usage(response.usage),
            )
            if response.stop_reason not in {"error", "aborted"}:
                # s11 新增：错误状态已经由 model_response 计过失败次数，避免重复。
                self.emit(AgentEvent(type="summary_empty", data={"model": response.model}))
        return SummaryResult(None, None, None)


# s11 修改：相对 s07，压缩器把成功摘要的用量随 CompactionEntry 落盘。
class ContextCompactor:
    """执行 s07 的上下文压缩，并把摘要请求的用量写入压缩记录。

    输入：会话、压缩策略和返回 SummaryResult 的摘要器。
    输出：未触发压缩时返回 None；触发后返回压缩前后大小等结果。
    流程：准备计划 → 调用摘要器 → 必要时回退 → 限制长度 → 保存摘要与用量。
    """

    def __init__(
        self,
        session: SessionManager,
        policy: CompactionPolicy,
        summarize: ContextSummarizer,
    ) -> None:
        self.session = session
        self.policy = policy
        self.summarize = summarize

    def compact_if_needed(self) -> s07.CompactionOutcome | None:
        """压缩超限上下文，并让每次摘要请求随本次压缩持久化。"""
        plan = s07.prepare_compaction(self.session.load_entries(), self.policy)
        if plan is None:
            return None

        summary_result = self.summarize(plan.messages_to_summarize, plan.previous_summary)
        if summary_result.text is None:
            summary = s07._fallback_summary(
                self.session.path,
                plan.messages_to_summarize,
                plan.previous_summary,
            )
            summary_kind: Literal["model", "fallback"] = "fallback"
        else:
            summary = summary_result.text
            summary_kind = "model"

        summary = s07._fit_summary_to_budget(
            summary,
            plan.retained_tail,
            self.policy.max_context_chars,
            self.session.path,
        )
        after_chars = s07.estimate_context_chars(s07._summary_context(summary, plan.retained_tail))
        if after_chars > self.policy.max_context_chars:
            # s11 修正：压缩校验失败时没有 CompactionEntry，已发生的摘要用量单独落盘。
            if summary_result.model is not None:
                self.session.append_model_error(
                    summary_result.model,
                    "summary",
                    summary_result.usage,
                )
            raise RuntimeError("压缩后上下文仍超过字符阈值")

        # s11 修改：参考 Pi，成功摘要的用量随 CompactionEntry 保存；空响应已单独落盘。
        self.session.append_compaction(
            summary,
            plan.retained_tail,
            summary_model=summary_result.model,
            summary_usage=summary_result.usage,
        )
        return s07.CompactionOutcome(
            summary_kind=summary_kind,
            before_chars=plan.before_chars,
            after_chars=after_chars,
            summarized_messages=len(plan.messages_to_summarize),
            retained_messages=len(plan.retained_tail),
        )


# 来自 s10：保持；压缩仍在正常回答请求前完成，并报告 s11 运行事件。
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
    save_assistant: AssistantSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行可追踪、可统计的流式多工具 Agent 循环。

    作用：保持 s10 的 Provider—工具闭环，并为一次任务发出完整的运行事实。
    输入：消息、模型请求函数、工具分发、系统提示词、Hooks、EventSink 和两类保存函数。
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
        # s11 修改：assistant 内容与 ModelResponse 的 usage 同时写入会话。
        if save_assistant is not None:
            save_assistant(assistant_message, response)
        elif save_message is not None:
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        # s11 修改：Pi 的失败响应也有用量；已保存消息后结束本轮，不执行其工具块。
        if response.stop_reason in {"error", "aborted"}:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        tool_calls = [
            block for block in assistant_message["content"] if _get(block, "type") == "tool_use"
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

    作用：组装会话记录、终端、Trace 和本轮统计，再组装模型与压缩流程。
    输入：s05 保持的 `--session` 参数，以及终端中的自然语言任务。
    输出：流式回答、本轮与会话累计统计、会话 JSONL 和紧凑 Trace JSONL。
    流程：加载配置 → 打开会话 → 组装观察层与模型层 → 组装压缩层
    → 注入 agent_loop → 从会话记录重新计算累计用量。
    """
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))

    # s11 修改：直接复用 s10 Provider，统计逻辑不再绑定 Anthropic SDK。
    provider = create_model_provider(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )

    # s11 修改：失败记录必须绑定当前会话，先打开 Session 再组装观察层。
    session_path = s05.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)

    # s11 修改：事件消费者并列；失败调用直接写会话，正常回答在 agent_loop 落盘。
    terminal = TerminalEventSink()
    trace_path = new_trace_path()
    trace_recorder = JsonlTraceRecorder(trace_path)
    usage_tracker = UsageTracker()
    error_recorder = SessionErrorRecorder(session)
    events = EventDispatcher()
    events.subscribe(terminal)
    events.subscribe(trace_recorder)
    events.subscribe(usage_tracker)
    events.subscribe(error_recorder)

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

    # s11 修改：摘要器把每次响应的 usage 显式传给压缩器，随后进入 CompactionEntry。
    policy = CompactionPolicy(
        max_context_chars=_positive_int_env("SESSION_COMPACTION_MAX_CHARS", 24000),
        keep_recent_chars=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_CHARS", 12000),
    )
    summary_max_tokens = _positive_int_env("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", 1024)
    summarizer = ContextSummarizer(
        create_message=summary_requester,
        max_tokens=summary_max_tokens,
        session=session,
        emit=events.emit,
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
                save_assistant=session.append_assistant,
            )
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
        finally:
            # s11 修改：本轮来自事件，会话累计来自完整 JSONL，恢复会话后仍然成立。
            print(usage_tracker.summary_text())
            print(session.session_stats().summary_text())
            print()


if __name__ == "__main__":
    main()
