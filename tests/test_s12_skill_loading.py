import json
from pathlib import Path

import pytest

from s12_skill_loading import code as chapter


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
