#!/usr/bin/env python3
"""s12：能力系统 · Skills 按需加载。

启动时只将技能名称、描述和路径交给模型；匹配任务时通过 read_file 读取正文。
技能正文作为普通工具结果进入会话，继续使用 s11 的权限、压缩、事件和用量链路。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml
from dotenv import load_dotenv

from s05_session_persistence import code as s05
from s11_runtime_observability import code as previous
from zero2pi.ui import format_error, format_user_prompt

# ===== 来自 s11：运行时与工具依赖（保持） =====
# 会话、模型请求、工具、权限和观察者复用 s11；本章只增加技能目录。

Message = previous.Message
ModelResponse = previous.ModelResponse
DispatchTool = previous.DispatchTool
Hooks = previous.Hooks
SessionSaver = previous.SessionSaver
AssistantSaver = previous.AssistantSaver
EventSink = previous.EventSink
AgentEvent = previous.AgentEvent
SessionManager = previous.SessionManager
CompactionPolicy = previous.CompactionPolicy
ContextCompactor = previous.ContextCompactor
CompactedContextRequester = previous.CompactedContextRequester
BlockingModelRequester = previous.BlockingModelRequester
StreamingModelRequester = previous.StreamingModelRequester
EventDispatcher = previous.EventDispatcher
TerminalEventSink = previous.TerminalEventSink
JsonlTraceRecorder = previous.JsonlTraceRecorder
UsageTracker = previous.UsageTracker
SessionErrorRecorder = previous.SessionErrorRecorder
SYSTEM = previous.SYSTEM
execute_tool = previous.execute_tool
make_permission_hook = previous.make_permission_hook
create_model_provider = previous.create_model_provider
# 来自 s11：保持；摘要参数和观察者均复用运行时，不属于 Skills 机制。
summary_request_options = previous.summary_request_options
_positive_int_env = previous._positive_int_env
_get = previous._get
_tool_result_is_error = previous._tool_result_is_error

# s12 修改：会话与 Trace 继续按章节隔离，避免与 s11 数据混用。
SESSION_ROOT = Path(".sessions/s12")
TRACE_ROOT = Path(".traces/s12")
# s12 新增：只发现本章项目内的技能，目录相对启动工作区解析。
SKILLS_ROOT = Path("s12_skill_loading/skills")
# 来自 s11：保持；工具预算、摘要策略与运行保护不属于 Skills，直接复用。
TOOLS = previous.TOOLS
dispatch_tool = previous.dispatch_tool
ContextSummarizer = previous.ContextSummarizer
show_summary_diagnostic = previous.show_summary_diagnostic
model_round_limit_from_env = previous.model_round_limit_from_env


# ===== s12 新增：技能目录 =====


# s12 新增：参考 Pi 保存路径而非缓存正文，让 read_file 负责按需加载。
@dataclass(frozen=True)
class Skill:
    """描述一项可供模型选择的技能，不保存 Markdown 正文。

    输入：技能头部的名称、描述，以及解析后的 SKILL.md 路径。
    输出：不可变目录项，交给提示词组装函数使用。
    流程：SkillCatalog.scan() 创建 → build_system_prompt() 提供目录 → 模型读取文件。
    """

    name: str
    description: str
    path: Path


def _read_skill_metadata(path: Path) -> dict[str, Any]:
    """只读取文件开头的 YAML 头部，遇到结束分隔线即停止。"""
    with path.open(encoding="utf-8-sig") as file:
        if file.readline().strip() != "---":
            raise ValueError(f"技能缺少 YAML 头部：{path}")
        header: list[str] = []
        for line in file:
            if line.strip() == "---":
                break
            header.append(line)
        else:
            raise ValueError(f"技能 YAML 头部未闭合：{path}")
    try:
        metadata = yaml.safe_load("".join(header))
    except yaml.YAMLError as error:
        raise ValueError(f"技能 YAML 无效：{path}") from error
    if not isinstance(metadata, dict):
        raise TypeError(f"技能 YAML 必须是对象：{path}")
    return metadata


# s12 新增：参考 lcc 的单层技能目录，扫描与按需读取分开，保持教学实现可读。
@dataclass(frozen=True)
class SkillCatalog:
    """发现本章技能，并保存模型选择技能所需的最少信息。

    输入：包含 `<名称>/SKILL.md` 的根目录。
    输出：按路径排序的 Skill 元组；根目录不存在时返回空目录。
    流程：发现文件 → 读取 YAML 头部 → 校验名称与描述 → 收集目录项。
    边界：不读正文、不执行脚本、不注册工具；格式错误直接报告文件位置。
    """

    skills: tuple[Skill, ...]

    @classmethod
    def scan(cls, root: Path) -> SkillCatalog:
        """扫描项目内技能头部，并拒绝无效目录项。

        输入：技能根目录，相对路径按当前工作目录解析。
        输出：只包含 name、description、path 的 SkillCatalog。
        流程：按路径排序发现 SKILL.md → 校验工作区边界与必需字段 → 返回目录。
        """
        skills_root = root.resolve()
        workspace = Path.cwd().resolve()
        if not skills_root.is_relative_to(workspace):
            raise ValueError("技能目录必须位于工作区内，才能使用 read_file 加载")
        if not skills_root.exists():
            return cls(skills=())
        if not skills_root.is_dir():
            raise ValueError(f"技能根路径必须是目录：{root}")

        skills: list[Skill] = []
        for manifest in sorted(skills_root.glob("*/SKILL.md")):
            path = manifest.resolve()
            if not path.is_relative_to(skills_root):
                raise ValueError(f"技能文件越过根目录：{manifest}")
            metadata = _read_skill_metadata(path)
            name = metadata.get("name")
            description = metadata.get("description")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or len(name) > 64
                or name != manifest.parent.name
            ):
                raise ValueError(f"技能名称须与目录一致，使用小写字母、数字和连字符：{manifest}")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"技能缺少非空 description：{manifest}")
            skills.append(Skill(name, description.strip(), path))
        return cls(skills=tuple(skills))


# s12 新增：参考 Pi，模型先看名称、描述和路径，再用已有 read_file 读取全文。
def build_system_prompt(base_system: str, catalog: SkillCatalog) -> str:
    """将技能目录附加到基础提示词，不加入技能正文。

    输入：全局 SYSTEM 中的基础指令，以及启动时扫描得到的目录。
    输出：实际发送给回答模型的系统提示词；无技能时原样返回基础指令。
    流程：将目录项序列化为 JSON → 附加简短加载规则 → 交给 agent_loop 的 system 参数。
    """
    if not catalog.skills:
        return base_system
    entries = [
        {"name": skill.name, "description": skill.description, "path": skill.path.as_posix()}
        for skill in catalog.skills
    ]
    return (
        base_system
        + "\n\n可用技能：\n"
        + json.dumps(entries, ensure_ascii=False, indent=2)
        + "\n任务匹配技能描述时，先用 read_file 完整读取对应 SKILL.md，再按说明处理任务。"
        + "技能中的相对路径以 SKILL.md 所在目录为基准。"
    )


# ===== 来自 s11：完整核心循环（保持，含请求轮数保护） =====
# s12 的技能读取仍属于普通工具调用，不在循环中增加技能专用分支。


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
    # 来自 s11：保持；限轮仅在调用方明确配置时启用，不是默认完成策略。
    max_rounds: int | None = None,
) -> list[Message]:
    """运行带工具调用、会话保存和事件观察的 Agent 循环。

    输入：模型消息、请求器、工具分发器、含技能目录的 system、Hooks 和保存回调。
    输出：本次运行的完整消息；没有工具调用或模型返回失败状态时结束，超限时报错。
    流程：请求模型 → 保存 assistant 与 usage → 执行工具 → 保存结果 → 再次请求。
    技能正文与其他 read_file 结果一样进入会话，循环不决定模型选择哪项技能。
    """
    # 来自 s11：保持；轮数只计算回答请求；摘要由请求包装器负责，不隐含在计数中。
    # 来自 s11：保持；默认不限制轮数，有明确配置时才停止。
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max_rounds 必须为正整数")
    emit(AgentEvent(type="agent_start", data={"message_count": len(messages)}))

    # 来自 s11：保持；显式统计回答请求，None 时不设置硬停止点。
    round_count = 0
    while max_rounds is None or round_count < max_rounds:
        round_count += 1
        # 来自 s11：保持；摘要或回答请求失败同样结束本次任务，观察者不会收到悬空生命周期。
        try:
            response = create_message(
                system=system,
                messages=messages,
                tools=tools or TOOLS,
                max_tokens=max_tokens,
            )
        except (RuntimeError, ValueError):
            emit(
                AgentEvent(
                    type="agent_end",
                    data={"messages": list(messages), "reason": "request_failed"},
                )
            )
            raise
        assistant_message = {
            "role": "assistant",
            "content": s05._to_jsonable(response.content),
        }
        messages.append(assistant_message)
        if save_assistant is not None:
            save_assistant(assistant_message, response)
        elif save_message is not None:
            save_message(assistant_message)
        emit(AgentEvent(type="assistant_message", data={"message": assistant_message}))

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
            emit(
                AgentEvent(
                    type="tool_call",
                    data={"tool_call_id": tool_call_id, "name": name, "arguments": arguments},
                )
            )

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
            results.append({"type": "tool_result", "tool_use_id": tool_call_id, "content": output})

        tool_message = {"role": "user", "content": results}
        messages.append(tool_message)
        if save_message is not None:
            save_message(tool_message)

    # 来自 s11：保持；保留已完成工具结果并报告未完成，不伪造一次模型最终回答。
    emit(
        AgentEvent(
            type="agent_end",
            data={"messages": list(messages), "reason": "max_rounds"},
        )
    )
    raise RuntimeError(f"已达到 {max_rounds} 轮模型请求上限，任务尚未完成；已停止自动调用工具。")


# ===== s12 修改：入口组装技能目录与实际系统提示词 =====


def main(arguments: Sequence[str] | None = None) -> None:
    """启动支持按需读取技能说明的 Agent。

    输入：模型环境配置、项目技能目录、可选 --session 参数和终端任务。
    输出：回答、工具结果、本轮与会话累计统计，以及 s12 独立会话和 Trace。
    流程：基础依赖 → 扫描技能 → 组装提示词与观察者 → 组装请求链 → 交互循环。
    """
    # ===== 来自 s11：基础依赖（保持） =====
    load_dotenv(override=True)
    timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    max_retries = int(os.getenv("MODEL_MAX_RETRIES", "0"))
    provider = create_model_provider(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    session_path = s05.session_path_from_cli(arguments, session_root=SESSION_ROOT)
    session = SessionManager.open(session_path)

    # s12 新增：先创建目录组件，再明确组装实际传给回答模型的 system。
    skill_catalog = SkillCatalog.scan(SKILLS_ROOT)
    system_prompt = build_system_prompt(SYSTEM, skill_catalog)

    # ===== 来自 s11：观察层与模型层（保持） =====
    terminal = TerminalEventSink()
    # s12 修改：调用 s11 的路径生成器时传入本章目录，避免 Trace 写回 s11。
    trace_path = previous.new_trace_path(TRACE_ROOT)
    trace_recorder = JsonlTraceRecorder(trace_path)
    usage_tracker = UsageTracker()
    error_recorder = SessionErrorRecorder(session)
    events = EventDispatcher()
    events.subscribe(terminal)
    events.subscribe(trace_recorder)
    events.subscribe(usage_tracker)
    events.subscribe(error_recorder)
    # 来自 s11：保持；空摘要诊断属于运行时观察层，本章直接复用。
    events.subscribe(show_summary_diagnostic)

    summary_requester = BlockingModelRequester(provider, timeout_seconds, events.emit)
    assistant_requester = StreamingModelRequester(provider, timeout_seconds, events.emit)

    # ===== 来自 s11：上下文与运行时组装（保持） =====
    policy = CompactionPolicy(
        # 来自 s07：保持；模型窗口、预留和近期预算分别组装，不再读取旧字符阈值。
        context_window_tokens=_positive_int_env("MODEL_CONTEXT_WINDOW_TOKENS", 0),
        reserve_tokens=_positive_int_env("SESSION_COMPACTION_RESERVE_TOKENS", 16384),
        keep_recent_tokens=_positive_int_env("SESSION_COMPACTION_KEEP_RECENT_TOKENS", 20000),
    )
    # 来自 s07：保持；摘要默认额度来自预留预算，不改变正常回答请求的额度。
    summary_default_tokens = int(policy.reserve_tokens * 0.8)
    summary_max_tokens = _positive_int_env(
        "SESSION_COMPACTION_SUMMARY_MAX_TOKENS", summary_default_tokens
    )
    # 来自 s11：保持；摘要请求策略由 s07 定义、s10 适配，本章只组装已有组件。
    summary_options = summary_request_options(provider)
    summarizer = ContextSummarizer(
        create_message=summary_requester,
        max_tokens=summary_max_tokens,
        session=session,
        emit=events.emit,
        request_options=summary_options,
    )
    compactor = ContextCompactor(session=session, policy=policy, summarize=summarizer)
    request_with_context = CompactedContextRequester(
        create_message=assistant_requester,
        session=session,
        compactor=compactor,
        emit=events.emit,
    )
    hooks = Hooks(before_tool_call=[make_permission_hook()])
    # 来自 s11：保持；运行保护独立于技能加载，通过参数显式注入。
    round_limit = model_round_limit_from_env()

    # s12 修改：交互入口显示技能数，用户可观察是否扫描成功。
    print("s12：能力系统 · Skills 按需加载")
    print(f"Provider：{os.getenv('MODEL_PROVIDER', 'anthropic')}")
    print(f"模型：{provider.model}")
    print(f"会话文件：{session.path}")
    print(f"Trace 文件：{trace_path}")
    # 来自 s11：保持；展示已组装的预算与可选上限，不增加技能专属配置。
    print(
        f"上下文配置：窗口 {policy.context_window_tokens}，触发 {policy.trigger_tokens}，近期保留 {policy.keep_recent_tokens} Token。"
    )
    print(f"回答轮数上限：{round_limit if round_limit is not None else '不限制'}")
    print(f"可用技能：{len(skill_catalog.skills)} 项")
    print("输入任务，输入 q 退出。\n")

    while True:
        try:
            # s12 修改：提示符使用本章编号。
            query = input(format_user_prompt("s12")).strip()
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
                # s12 修改：SYSTEM 仍是全局基础指令；运行时传入含技能目录的提示词。
                system=system_prompt,
                hooks=hooks,
                emit=events.emit,
                save_message=session.append_message,
                save_assistant=session.append_assistant,
                # 来自 s11：保持；未配置上限时不会在第 12 轮直接退出。
                max_rounds=round_limit,
            )
        except (RuntimeError, ValueError) as error:
            print(format_error(str(error)), file=sys.stderr)
        finally:
            print(usage_tracker.summary_text())
            print(session.session_stats().summary_text())
            print()


if __name__ == "__main__":
    main()
