import json
import re
from pathlib import Path

import pytest

from s12_skill_loading import code as chapter


def test_skills_chapter_reuses_runtime_summary_and_diagnostics():
    assert chapter.ContextSummarizer is chapter.previous.ContextSummarizer
    assert chapter.show_summary_diagnostic is chapter.previous.show_summary_diagnostic
    assert chapter.model_round_limit_from_env is chapter.previous.model_round_limit_from_env


def _skill(root: Path, name: str, header: str, body: str = "正文专用标记") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\n{header}\n---\n{body}\n", encoding="utf-8")
    return path


def test_catalog_exposes_only_metadata_and_prompt_does_not_include_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "skills"
    path = _skill(root, "review", "name: review\ndescription: 审查指定文件")
    catalog = chapter.SkillCatalog.scan(root)

    assert catalog.skills == (chapter.Skill("review", "审查指定文件", path.resolve()),)
    prompt = chapter.build_system_prompt("基础指令", catalog)
    assert path.as_posix() in prompt
    assert "审查指定文件" in prompt
    assert "正文专用标记" not in prompt
    assert "read_file" in prompt


def test_missing_skills_directory_keeps_base_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = chapter.SkillCatalog.scan(tmp_path / "missing")
    assert catalog.skills == ()
    assert chapter.build_system_prompt("基础指令", catalog) == "基础指令"


@pytest.mark.parametrize(
    "header",
    ["name: review", "name: other\ndescription: 审查代码", "name: review\ndescription: 123"],
)
def test_invalid_metadata_is_reported_with_file_path(tmp_path, monkeypatch, header):
    monkeypatch.chdir(tmp_path)
    path = _skill(tmp_path / "skills", "review", header)
    with pytest.raises(ValueError, match="SKILL.md"):
        chapter.SkillCatalog.scan(path.parent.parent)


def test_yaml_header_requires_closing_separator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "skills" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: review\ndescription: 审查代码\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未闭合"):
        chapter.SkillCatalog.scan(path.parent.parent)


def test_skill_root_outside_workspace_is_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with pytest.raises(ValueError, match="工作区"):
        chapter.SkillCatalog.scan(tmp_path / "outside")


def test_main_loads_skill_via_existing_read_tool_and_persists_result(tmp_path, monkeypatch):
    requests: list[dict] = []
    manifest = chapter.SKILLS_ROOT / "code-review" / "SKILL.md"
    skill_text = manifest.read_text(encoding="utf-8")

    class Provider:
        model = "offline-test"

        def stream(self, *, on_text, **kwargs):
            requests.append(kwargs)
            assert "code-review" in kwargs["system"]
            assert "优先检查行为错误" not in kwargs["system"]
            if len(requests) == 1:
                content = [
                    {
                        "type": "tool_use",
                        "id": "skill-read",
                        "name": "read_file",
                        "input": {"path": manifest.resolve().as_posix()},
                    }
                ]
            else:
                result = kwargs["messages"][-1]["content"][0]
                assert result["tool_use_id"] == "skill-read"
                assert "优先检查行为错误" in result["content"]
                content = [{"type": "text", "text": "已读取代码审查技能"}]
            return chapter.ModelResponse(
                content=content,
                model=self.model,
                stop_reason="tool_use" if len(requests) == 1 else "end_turn",
                usage=chapter.previous.ModelUsage(input_tokens=10, output_tokens=2),
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions" / "s12")
    monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces" / "s12")
    inputs = iter(["按 code-review 技能审查指定代码", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    chapter.main([])

    session_path = next((tmp_path / "sessions" / "s12").glob("*.jsonl"))
    session = chapter.SessionManager.open(session_path)
    messages = session.build_context()
    assert len(requests) == 2
    assert skill_text.rstrip() == messages[2]["content"][0]["content"]
    assert session.session_stats().known_tokens == 24
    assert session.session_stats().tool_calls == 1
    trace_path = next((tmp_path / "traces" / "s12").glob("*.jsonl"))
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert any(record["type"] == "tool_call" for record in records)


def test_skills_reuse_tool_definitions_and_dispatch_without_extra_limits():
    assert chapter.TOOLS is chapter.previous.TOOLS
    assert chapter.dispatch_tool is chapter.previous.dispatch_tool
    assert any(tool["name"] == "grep" for tool in chapter.TOOLS)


def test_review_entry_can_load_skill_locate_and_read_code_without_summary(tmp_path, monkeypatch):
    manifest = chapter.SKILLS_ROOT / "code-review" / "SKILL.md"
    source = "s12_skill_loading/code.py"
    requests = []
    target_line = None

    class Provider:
        model = "offline-review"

        def complete(self, **kwargs):
            pytest.fail("短审查不应触发摘要")

        def stream(self, *, on_text, **kwargs):
            nonlocal target_line
            requests.append(kwargs)
            if len(requests) == 1:
                name, arguments = "read_file", {"path": manifest.as_posix()}
            elif len(requests) == 2:
                assert "grep" in kwargs["messages"][-1]["content"][0]["content"]
                name, arguments = "grep", {"path": source, "pattern": "def scan"}
            elif len(requests) == 3:
                result = kwargs["messages"][-1]["content"][0]["content"]
                target_line = int(re.search(r"code.py:(\d+):", result).group(1))
                name, arguments = (
                    "read_file",
                    {"path": source, "start_line": target_line, "limit": 38},
                )
            else:
                result = kwargs["messages"][-1]["content"][0]["content"]
                assert "skills_root = root.resolve()" in result
                assert "return cls(skills=tuple(skills))" in result
                return chapter.ModelResponse(
                    content=[{"type": "text", "text": "已取得完整目标方法；未执行测试。"}],
                    model=self.model,
                    stop_reason="end_turn",
                    usage=chapter.previous.ModelUsage(input_tokens=100, output_tokens=20),
                )
            return chapter.ModelResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": f"call-{len(requests)}",
                        "name": name,
                        "input": arguments,
                    }
                ],
                model=self.model,
                stop_reason="tool_use",
                usage=chapter.previous.ModelUsage(input_tokens=100, output_tokens=20),
            )

    monkeypatch.setattr(chapter, "create_model_provider", lambda **kwargs: Provider())
    monkeypatch.setattr(chapter, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "128000")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "0")
    monkeypatch.setattr(chapter, "SESSION_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(chapter, "TRACE_ROOT", tmp_path / "traces")
    inputs = iter(["按 code-review 技能只读审查 SkillCatalog.scan", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    chapter.main([])
    assert len(requests) == 4
    records = [
        json.loads(line)
        for line in next((tmp_path / "sessions").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert all(record["type"] == "message" for record in records)


def test_repeated_tool_calls_stop_at_round_budget_and_keep_results(tmp_path):
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    session.append_message({"role": "user", "content": "只读审查"})
    requests = []
    executed = []
    events = []

    def request(**kwargs):
        requests.append(kwargs)
        return chapter.ModelResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": f"call-{len(requests)}",
                    "name": "glob",
                    "input": {"pattern": "*.py"},
                }
            ],
            model="offline-test",
            stop_reason="tool_use",
            usage=None,
        )

    def dispatch(name, arguments):
        executed.append((name, arguments))
        return "code.py"

    with pytest.raises(RuntimeError, match="任务尚未完成"):
        chapter.agent_loop(
            session.build_context(),
            create_message=request,
            dispatch=dispatch,
            system="test",
            hooks=chapter.Hooks(),
            emit=events.append,
            save_message=session.append_message,
            save_assistant=session.append_assistant,
            max_rounds=3,
        )
    assert len(requests) == len(executed) == 3
    assert len(session.build_context()) == 7
    assert session.build_context()[-1]["content"][0]["type"] == "tool_result"
    assert events[-1].type == "agent_end"
    assert events[-1].data["reason"] == "max_rounds"


def test_empty_summary_stops_before_compaction_and_assistant_request(tmp_path):
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    original = {"role": "user", "content": "审查目标代码" * 200}
    session.append_message(original)
    events = []
    summary_calls = []
    assistant_calls = []

    def summary_request(**kwargs):
        summary_calls.append(kwargs)
        return chapter.ModelResponse(
            content=[],
            model="offline-test",
            stop_reason="max_tokens",
            usage=chapter.previous.ModelUsage(input_tokens=100, output_tokens=1024),
        )

    def assistant_request(**kwargs):
        assistant_calls.append(kwargs)
        pytest.fail("空摘要后不应继续请求回答模型")

    summarizer = chapter.ContextSummarizer(
        create_message=summary_request,
        max_tokens=1024,
        session=session,
        emit=events.append,
    )
    compactor = chapter.ContextCompactor(
        session=session,
        policy=chapter.CompactionPolicy(
            context_window_tokens=314, reserve_tokens=64, keep_recent_tokens=100
        ),
        summarize=summarizer,
    )
    requester = chapter.CompactedContextRequester(
        create_message=assistant_request,
        session=session,
        compactor=compactor,
        emit=events.append,
    )
    with pytest.raises(RuntimeError, match="摘要模型未返回正文"):
        requester(system="test", messages=session.build_context(), tools=[])
    assert len(summary_calls) == 1
    assert assistant_calls == []
    assert chapter.SessionManager.open(session.path).build_context() == [original]
    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert [record["type"] for record in records] == ["message", "model_error"]
    assert records[-1]["usage"]["output_tokens"] == 1024


def test_successful_summary_keeps_s11_result_and_usage(tmp_path):
    session = chapter.SessionManager.open(tmp_path / "session.jsonl")
    usage = chapter.previous.ModelUsage(input_tokens=30, output_tokens=10)
    response = chapter.ModelResponse(
        content=[{"type": "text", "text": "代码审查证据摘要"}],
        model="offline-test",
        stop_reason="end_turn",
        usage=usage,
    )
    summarizer = chapter.ContextSummarizer(
        create_message=lambda **kwargs: response,
        max_tokens=1024,
        session=session,
        emit=lambda event: None,
    )
    result = summarizer([{"role": "user", "content": "审查代码"}], None)
    assert result.text == "代码审查证据摘要"
    assert result.model == "offline-test"
    assert result.usage == usage


def test_request_failure_ends_run_without_dispatching_tools():
    events = []

    def request(**kwargs):
        raise RuntimeError("摘要模型未返回正文")

    def dispatch(name, arguments):
        pytest.fail("请求失败后不应执行工具")

    with pytest.raises(RuntimeError, match="摘要模型未返回正文"):
        chapter.agent_loop(
            [{"role": "user", "content": "审查代码"}],
            create_message=request,
            dispatch=dispatch,
            system="test",
            hooks=chapter.Hooks(),
            emit=events.append,
        )
    assert [event.type for event in events] == ["agent_start", "agent_end"]
    assert events[-1].data["reason"] == "request_failed"
