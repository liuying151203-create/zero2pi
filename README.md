# zero2pi

`zero2pi` 是一个从零实现 Agent 的学习与实践项目：以 `learn-claude-code` 的渐进式方式组织开发，参考 `pi` 的 Agent loop、工具调用和运行时设计，逐步构建一个清晰、可运行、可扩展的 Agent Harness。

## 基础环境

下面的命令分别用于创建虚拟环境、安装项目依赖，以及创建本地配置文件：

```powershell
# 创建项目独立的 Python 虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 安装项目依赖和开发工具
python -m pip install -e ".[dev]"
# 创建本地环境变量文件
Copy-Item .env.example .env
```

在 `.env` 中填写模型密钥。若 PowerShell 不允许激活虚拟环境，可直接使用 `.venv\Scripts\python.exe`。

## 常用命令

```powershell
# 运行第一章：核心循环
python -m s01_agent_loop.code
# 运行第二章：工具调用
python -m s02_tool_use.code
# 运行第三章：工具权限
python -m s03_permission.code
# 运行第四章：工具调用 Hooks
python -m s04_hooks.code
# 运行第五章：会话系统 · 持久化
python -m s05_session_persistence.code
# 加载已有的第五章会话
python -m s05_session_persistence.code --session .sessions\s05\session-20260919-120000.jsonl
# 运行第六章：会话系统 · 记录与上下文投影
python -m s06_session_context.code
# 加载已有会话并按 s06 Entry 规则构建模型上下文
python -m s06_session_context.code --session .sessions\s06\session-20260919-120000.jsonl
# 运行第七章：会话系统 · 上下文压缩
python -m s07_session_compaction.code
# 加载已有的第七章会话
python -m s07_session_compaction.code --session .sessions\s07\session-20260919-120000.jsonl
# 运行测试
python -m pytest
# 检查代码规范
ruff check s01_agent_loop s02_tool_use s03_permission s04_hooks s05_session_persistence s06_session_context s07_session_compaction src tests
```
