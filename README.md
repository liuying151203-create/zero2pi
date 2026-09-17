# zero2pi

`zero2pi` 是一个从零实现 Agent 的学习与实践项目：以 `learn-claude-code` 的渐进式方式组织开发，参考 `pi` 的 Agent loop、工具调用和运行时设计，逐步构建一个清晰、可运行、可扩展的 Agent Harness。

## 基础环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在 `.env` 中填写模型密钥。若 PowerShell 不允许激活虚拟环境，可直接使用 `.venv\Scripts\python.exe`。

## 常用命令

```powershell
python -m pytest
ruff check src tests
python -m zero2pi.main
```
