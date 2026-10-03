#!/usr/bin/env python3
"""s10：模型系统 · Provider 边界。

s10 把不同模型 SDK 的请求和响应差异收进 Provider：

    统一 Message / Tools -> ModelProvider -> 统一 ModelResponse

Agent loop 只读取统一的文本块、工具调用和 usage，不再依赖具体 SDK 对象。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from anthropic import Anthropic
from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s09_runtime_streaming import code as previous
from zero2pi.ui import (
    format_assistant_message,
    format_error,
    format_model_request,
    format_tool_call,
    format_tool_result,
    format_user_prompt,
)

# s10 修改：Provider 章节使用独立会话目录，避免与 s09 的运行记录混用。
SESSION_ROOT = Path(".sessions/s10")

# ===== 来自 s09：Agent、工具与会话依赖（保持） =====
# s10 只替换模型请求边界，工具、Hooks、压缩算法和会话 Entry 格式保持不变。

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

# ===== 来自 s09：运行事件和终端展示（保持） =====

AgentEvent = previous.AgentEvent
EventSink = previous.EventSink


@dataclass
class TerminalEventSink:
    """把完整事件和文本增量渲染到终端。

    作用：保持 s09 的流式显示行为；Provider 只报告文本，不决定终端样式。
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


# ===== s10 新增：Provider 无关的响应模型 =====


@dataclass(frozen=True)
class ModelUsage:
    """保存一次模型响应中与 Provider 无关的 Token 用量。

    作用：屏蔽 Anthropic 和 OpenAI-compatible usage 字段名称差异。
    输入：输入、输出、缓存读取和缓存写入 Token 数。
    输出：Provider 统一返回该对象；`total_tokens` 给出总用量。
    流程：Provider 读取 SDK 响应 → 构造 ModelUsage → 放入 ModelResponse。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """返回输入、输出和缓存 Token 的总和。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def to_dict(self) -> dict[str, int]:
        """转成可写入事件或 JSON 的普通字典。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
        }


# s10 新增：Agent loop 只依赖该响应，不再读取 Anthropic/OpenAI SDK 类型。
@dataclass(frozen=True)
class ModelResponse:
    """描述一次已经归一化的模型响应。

    作用：向 Agent loop 提供统一内容块、模型信息、停止原因和 Token 用量。
    输入：Provider 从原始 SDK 响应转换得到的数据。
    输出：`content` 使用项目既有 `text` / `tool_use` 块，可直接保存和执行。
    流程：SDK 响应 → Provider 转换 → 请求器返回 → agent_loop 读取 content。
    """

    content: list[dict[str, Any]]
    model: str
    stop_reason: str | None
    usage: ModelUsage


class ModelProvider(Protocol):
    """定义 Agent 需要的阻塞与流式模型能力。"""

    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送阻塞请求并返回统一响应。"""
        ...

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """发送流式请求，报告文本片段并返回统一最终响应。"""
        ...


# ===== s10 新增：Anthropic Provider =====


@dataclass(frozen=True)
class AnthropicProvider:
    """把 Anthropic Messages API 适配为统一 ModelProvider。

    作用：保留 s09 的 Anthropic 请求方式，并把 SDK 响应转换为 `ModelResponse`。
    输入：Anthropic 客户端、模型名称和统一请求参数。
    输出：阻塞或流式的统一模型响应；流式期间通过 `on_text` 报告文本片段。
    流程：调用 Messages API → SDK 组装最终消息 → 转换内容和 usage。
    """

    client: Any
    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送 Anthropic 阻塞请求并归一化响应。"""
        response = self.client.messages.create(model=self.model, **kwargs)
        return _normalize_anthropic_response(response, self.model)

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """消费 Anthropic 文本流，并归一化 SDK 最终消息。"""
        with self.client.messages.stream(model=self.model, **kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    on_text(text)
            response = stream.get_final_message()
        return _normalize_anthropic_response(response, self.model)


def _normalize_anthropic_response(response: Any, fallback_model: str) -> ModelResponse:
    """把 Anthropic SDK 消息转成统一响应。"""
    usage = _get(response, "usage") or {}
    return ModelResponse(
        content=s05._to_jsonable(_get(response, "content") or []),
        model=str(_get(response, "model") or fallback_model),
        stop_reason=_optional_text(_get(response, "stop_reason")),
        usage=ModelUsage(
            input_tokens=int(_get(usage, "input_tokens") or 0),
            output_tokens=int(_get(usage, "output_tokens") or 0),
            cache_read_tokens=int(_get(usage, "cache_read_input_tokens") or 0),
            cache_write_tokens=int(_get(usage, "cache_creation_input_tokens") or 0),
        ),
    )


# ===== s10 新增：OpenAI-compatible Provider =====


@dataclass(frozen=True)
class OpenAIChatProvider:
    """把 OpenAI-compatible Chat Completions 适配为统一 ModelProvider。

    作用：支持 OpenAI、DeepSeek 等兼容接口，同时保持 Agent 内部消息格式不变。
    输入：兼容客户端、模型名称，以及项目内部 system/messages/tools 请求参数。
    输出：统一 `ModelResponse`；OpenAI function call 会转换为内部 `tool_use`。
    流程：转换消息和工具 schema → 调用 Chat Completions → 组装文本与工具增量
    → 转回项目内部内容块和 usage。
    """

    client: Any
    model: str

    def complete(self, **kwargs: Any) -> ModelResponse:
        """发送 OpenAI-compatible 阻塞请求并归一化响应。"""
        request = _to_openai_request(kwargs)
        response = self.client.chat.completions.create(model=self.model, **request)
        return _normalize_openai_response(response, self.model)

    def stream(
        self,
        *,
        on_text: Callable[[str], None],
        **kwargs: Any,
    ) -> ModelResponse:
        """消费 Chat Completions 增量并组装统一最终响应。"""
        request = _to_openai_request(kwargs)
        chunks = self.client.chat.completions.create(
            model=self.model,
            stream=True,
            stream_options={"include_usage": True},
            **request,
        )

        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        response_model = self.model
        stop_reason: str | None = None
        usage = ModelUsage()

        for chunk in chunks:
            response_model = str(_get(chunk, "model") or response_model)
            if chunk_usage := _get(chunk, "usage"):
                usage = _openai_usage(chunk_usage)

            choices = _get(chunk, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            stop_reason = _optional_text(_get(choice, "finish_reason")) or stop_reason
            delta = _get(choice, "delta") or {}

            if text := _get(delta, "content"):
                text = str(text)
                text_parts.append(text)
                on_text(text)

            # s10 新增：工具名称和 JSON 参数可能分散在多个流片段中，按 index 累加。
            for tool_delta in _get(delta, "tool_calls") or []:
                _append_openai_tool_delta(tool_calls, tool_delta)

        content = _openai_content_blocks("".join(text_parts), tool_calls.values())
        return ModelResponse(
            content=content,
            model=response_model,
            stop_reason=stop_reason,
            usage=usage,
        )


def _to_openai_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把项目内部请求参数转成 Chat Completions 参数。"""
    request = dict(kwargs)
    system = str(request.pop("system", ""))
    messages = request.pop("messages", [])
    tools = request.pop("tools", [])
    request["messages"] = _to_openai_messages(system, messages)
    # s10 新增：内部摘要不提供工具；此时省略字段，避免兼容服务拒绝空 tools 数组。
    if tools:
        request["tools"] = [_to_openai_tool(tool) for tool in tools]
    return request


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """把项目内部消息历史转换为 Chat Completions 消息。"""
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})

    for message in messages:
        role = str(message["role"])
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            converted.append(_to_openai_assistant_message(blocks))
            continue

        text_parts = [
            str(_get(block, "text"))
            for block in blocks
            if _get(block, "type") == "text" and _get(block, "text")
        ]
        if text_parts:
            converted.append({"role": role, "content": "\n".join(text_parts)})
        for block in blocks:
            if _get(block, "type") == "tool_result":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(_get(block, "tool_use_id") or ""),
                        "content": str(_get(block, "content") or ""),
                    }
                )
    return converted


def _to_openai_assistant_message(blocks: list[Any]) -> dict[str, Any]:
    """转换一条可能同时包含文本和工具调用的 assistant 消息。"""
    text = "\n".join(
        str(_get(block, "text"))
        for block in blocks
        if _get(block, "type") == "text" and _get(block, "text")
    )
    tool_calls = []
    for block in blocks:
        if _get(block, "type") != "tool_use":
            continue
        tool_calls.append(
            {
                "id": str(_get(block, "id") or ""),
                "type": "function",
                "function": {
                    "name": str(_get(block, "name") or ""),
                    "arguments": json.dumps(
                        _get(block, "input") or {},
                        ensure_ascii=False,
                    ),
                },
            }
        )

    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """把 Anthropic 风格工具 schema 转成 OpenAI function tool。"""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object"}),
        },
    }


def _normalize_openai_response(response: Any, fallback_model: str) -> ModelResponse:
    """把非流式 Chat Completions 响应转成统一响应。"""
    choices = _get(response, "choices") or []
    if not choices:
        raise RuntimeError("OpenAI-compatible 模型没有返回 choices")

    choice = choices[0]
    message = _get(choice, "message") or {}
    tool_calls: list[dict[str, str]] = []
    for index, tool_call in enumerate(_get(message, "tool_calls") or []):
        function = _get(tool_call, "function") or {}
        tool_calls.append(
            {
                "index": str(index),
                "id": str(_get(tool_call, "id") or ""),
                "name": str(_get(function, "name") or ""),
                "arguments": str(_get(function, "arguments") or "{}"),
            }
        )

    return ModelResponse(
        content=_openai_content_blocks(str(_get(message, "content") or ""), tool_calls),
        model=str(_get(response, "model") or fallback_model),
        stop_reason=_optional_text(_get(choice, "finish_reason")),
        usage=_openai_usage(_get(response, "usage") or {}),
    )


def _append_openai_tool_delta(
    tool_calls: dict[int, dict[str, str]],
    tool_delta: Any,
) -> None:
    """把一个工具调用增量追加到对应 index 的缓冲区。"""
    index = int(_get(tool_delta, "index") or 0)
    current = tool_calls.setdefault(
        index,
        {"index": str(index), "id": "", "name": "", "arguments": ""},
    )
    if tool_id := _get(tool_delta, "id"):
        current["id"] = str(tool_id)
    function = _get(tool_delta, "function") or {}
    if name := _get(function, "name"):
        current["name"] += str(name)
    if arguments := _get(function, "arguments"):
        current["arguments"] += str(arguments)


def _openai_content_blocks(
    text: str,
    tool_calls: Any,
) -> list[dict[str, Any]]:
    """把 OpenAI 文本和 function calls 转成内部内容块。"""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for tool_call in tool_calls:
        arguments_text = str(tool_call.get("arguments") or "{}")
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"OpenAI-compatible 工具参数不是有效 JSON：{arguments_text}"
            ) from error
        content.append(
            {
                "type": "tool_use",
                "id": str(tool_call.get("id") or ""),
                "name": str(tool_call.get("name") or ""),
                "input": arguments,
            }
        )
    return content


def _openai_usage(usage: Any) -> ModelUsage:
    """把 Chat Completions usage 转成统一 Token 用量。"""
    prompt_details = _get(usage, "prompt_tokens_details") or {}
    cached_tokens = int(_get(prompt_details, "cached_tokens") or 0)
    prompt_tokens = int(_get(usage, "prompt_tokens") or 0)
    return ModelUsage(
        input_tokens=max(0, prompt_tokens - cached_tokens),
        output_tokens=int(_get(usage, "completion_tokens") or 0),
        cache_read_tokens=cached_tokens,
    )


# ===== s10 新增：按环境配置创建 Provider =====


# s10 修复：接口专有字段由 Provider 层选择，s07 摘要策略不假设所有 API 都兼容。
def summary_request_options(provider: ModelProvider) -> dict[str, Any]:
    """为当前接口选择摘要请求选项，不修改正常回答配置。

    输入：已创建的 ModelProvider。
    输出：Anthropic 使用显式关闭思考；其他 Provider 返回空选项，不猜测其专有参数。
    流程：识别接口实现 → 选择摘要参数 → 由 main 传给 ContextSummarizer。
    边界：兼容服务可能不支持或忽略参数；不能仅凭模型名字判断接口能力。
    """
    if isinstance(provider, AnthropicProvider):
        return {"thinking": {"type": "disabled"}}
    return {}


def create_model_provider(
    *,
    timeout_seconds: float,
    max_retries: int,
) -> ModelProvider:
    """根据环境变量创建当前章节使用的模型 Provider。

    作用：把 Provider 选择和 SDK 客户端构造集中在程序入口边界。
    输入：统一超时时间、重试次数，以及 `.env` 中的 Provider、模型和密钥配置。
    输出：`AnthropicProvider` 或 `OpenAIChatProvider`。
    流程：读取 MODEL_PROVIDER → 校验 MODEL_ID → 创建对应客户端 → 返回 Provider。
    """
    provider_name = os.getenv("MODEL_PROVIDER", "anthropic").strip().lower()
    model = os.getenv("MODEL_ID", "").strip()
    if not model:
        raise RuntimeError("请先在 .env 中设置 MODEL_ID。")

    if provider_name == "anthropic":
        client_options: dict[str, Any] = {
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }
        if api_key := os.getenv("ANTHROPIC_API_KEY"):
            client_options["api_key"] = api_key
        if base_url := os.getenv("ANTHROPIC_BASE_URL"):
            client_options["base_url"] = base_url
        return AnthropicProvider(Anthropic(**client_options), model)

    if provider_name == "openai-compatible":
        # s10 新增：延迟导入可让 Anthropic 用户在未安装可选 SDK 时仍得到清晰错误。
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError(
                "使用 openai-compatible Provider 前请按 README 安装项目依赖。"
            ) from error

        api_key = os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("openai-compatible Provider 需要 MODEL_API_KEY。")
        client_options = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            "max_retries": max_retries,
        }
        if base_url := os.getenv("MODEL_BASE_URL") or os.getenv("OPENAI_BASE_URL"):
            client_options["base_url"] = base_url
        return OpenAIChatProvider(OpenAI(**client_options), model)

    raise RuntimeError(
        f"MODEL_PROVIDER 仅支持 anthropic 或 openai-compatible，当前值：{provider_name}"
    )


# ===== s10 修改：请求器只依赖 ModelProvider =====


@dataclass(frozen=True)
class BlockingModelRequester:
    """通过 Provider 发送不需要增量展示的内部请求。

    作用：供上下文摘要使用，不暴露具体 SDK，也不产生用户可见文本流。
    输入：ModelProvider、超时时间、EventSink 和统一模型请求参数。
    输出：Provider 返回的 `ModelResponse`。
    流程：报告请求开始 → provider.complete → 返回统一响应；异常统一包装。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """发送阻塞 Provider 请求。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={"timeout_seconds": self.timeout_seconds},
            )
        )
        try:
            return self.provider.complete(**kwargs)
        except Exception as error:
            self.emit(AgentEvent(type="model_error", data={"message": str(error)}))
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error


@dataclass(frozen=True)
class StreamingModelRequester:
    """通过 Provider 流式请求正常回答。

    作用：统一 Anthropic 与 OpenAI-compatible 的流式入口，并保持 s09 事件展示。
    输入：ModelProvider、超时时间、EventSink 和统一模型请求参数。
    输出：Provider 组装完成的 `ModelResponse`。
    流程：报告请求开始 → Provider 发送文本片段 → 返回统一最终响应；异常统一包装。
    """

    provider: ModelProvider
    timeout_seconds: float
    emit: EventSink

    def __call__(self, **kwargs: Any) -> ModelResponse:
        """发送流式 Provider 请求并返回最终统一响应。"""
        self.emit(
            AgentEvent(
                type="model_request",
                data={"timeout_seconds": self.timeout_seconds},
            )
        )
        try:
            return self.provider.stream(on_text=self._emit_text, **kwargs)
        except Exception as error:
            self.emit(AgentEvent(type="model_error", data={"message": str(error)}))
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"模型请求失败：{error}") from error

    def _emit_text(self, text: str) -> None:
        """把 Provider 文本片段转换为 s09 保持的增量事件。"""
        self.emit(AgentEvent(type="assistant_delta", data={"text": text}))


# 来自 s09：保持；压缩只依赖统一请求函数，不感知底层 Provider。
@dataclass(frozen=True)
class CompactedContextRequester:
    """请求模型前按需压缩上下文，并报告压缩结果。

    作用：保持 s09 的压缩边界，把最新上下文交给 Provider 请求器。
    输入：底层请求函数、当前会话、压缩器、EventSink 和统一请求参数。
    输出：底层请求器返回的 `ModelResponse`。
    流程：尝试压缩 → 报告压缩 → 重建上下文 → 发起正常回答请求。
    """

    create_message: Callable[..., ModelResponse]
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


# ===== s10 修改：核心循环读取统一 ModelResponse =====
# 每个章节继续展开完整 agent_loop；s10 的循环不再接触具体 SDK 响应对象。


def _get(value: Any, name: str) -> Any:
    """兼容读取对象、字典和测试替身中的字段。"""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text(value: Any) -> str | None:
    """把可选值规范为字符串或 None。"""
    return None if value is None else str(value)


def agent_loop(
    messages: list[Message],
    *,
    create_message: Callable[..., ModelResponse],
    dispatch: DispatchTool,
    system: str,
    hooks: Hooks,
    emit: EventSink,
    save_message: SessionSaver | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 8000,
) -> list[Message]:
    """运行与具体 Provider 解耦的流式多工具 Agent 循环。

    作用：只使用统一 `ModelResponse` 完成消息保存、工具执行和后续模型请求。
    输入：消息、Provider 请求函数、工具分发、系统提示词、Hooks、事件和保存函数。
    输出：包含本次执行过程的完整消息列表；模型不再调用工具时返回。
    流程：请求 Provider → 保存统一 content → 执行 tool_use → 保存 tool_result
    → 继续请求；没有工具调用时发出 agent_end。
    """
    while True:
        # s10 修改：create_message 无论来自哪个 Provider，都返回同一种 ModelResponse。
        response = create_message(
            system=system,
            messages=messages,
            tools=tools or TOOLS,
            max_tokens=max_tokens,
        )
        assistant_message = {
            "role": "assistant",
            "content": response.content,
        }
        messages.append(assistant_message)
        if save_message is not None:
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

        tool_calls = [
            block for block in assistant_message["content"] if _get(block, "type") == "tool_use"
        ]
        if not tool_calls:
            emit(AgentEvent(type="agent_end", data={"messages": list(messages)}))
            return messages

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            name = str(_get(block, "name") or "")
            arguments = _get(block, "input") or {}
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
                    "tool_use_id": str(_get(block, "id") or ""),
                    "content": output,
                }
            )

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)


# ===== s10 修改：按 Provider、观察、上下文和循环层显式组装 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动可切换模型 Provider 的流式 Agent。

    作用：根据环境选择 Provider，再把统一请求器接入已有会话、压缩和 Agent loop。
    输入：`.env` Provider 配置、s05 保持的 `--session` 参数和终端自然语言任务。
    输出：与 s09 一致的流式回答、工具状态和会话文件。
    流程：加载配置 → 创建 Provider → 组装阻塞/流式请求器 → 组装会话与压缩
    → 注入 agent_loop → 持续读取用户任务。
    """
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))

    # s10 新增：Provider 是模型协议差异的唯一入口，其余组件只接收统一响应。
    provider = create_model_provider(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    terminal = TerminalEventSink()
    summary_requester = BlockingModelRequester(provider, timeout_seconds, terminal)
    assistant_requester = StreamingModelRequester(provider, timeout_seconds, terminal)

    # 来自 s09：保持；会话、压缩和权限组件不因 Provider 切换而改变。
    session_path = s05.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)
    policy = CompactionPolicy(
        max_context_chars=_positive_int_env("SESSION_COMPACTION_MAX_CHARS", 24000),
        keep_recent_chars=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_CHARS", 12000),
    )
    summary_max_tokens = _positive_int_env("SESSION_COMPACTION_SUMMARY_MAX_TOKENS", 1024)
    # s10 修复：摘要选项单独组装，OpenAI-compatible 不接收 Anthropic 的 thinking 字段。
    summary_options = summary_request_options(provider)
    summarizer = ContextSummarizer(
        create_message=summary_requester,
        max_tokens=summary_max_tokens,
        # s10 修复：仅摘要使用这组选项，主 Agent 的 assistant_requester 保持不变。
        request_options=summary_options,
    )
    compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
    request_with_context = CompactedContextRequester(
        create_message=assistant_requester,
        session=session,
        compactor=compactor,
        emit=terminal,
    )
    hooks = Hooks(before_tool_call=[make_permission_hook()])

    print("s10：模型系统 · Provider 边界")
    print(f"Provider：{os.getenv('MODEL_PROVIDER', 'anthropic')}")
    print(f"模型：{provider.model}")
    print(f"会话文件：{session.path}")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            query = input(format_user_prompt("s10")).strip()
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
                emit=terminal,
                save_message=session.append_message,
            )
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
            continue
        print()


if __name__ == "__main__":
    main()
