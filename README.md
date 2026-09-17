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
python s01_agent_loop/code.py
# 运行第二章：工具调用
python s02_tool_use/code.py
# 运行第三章：工具权限
python s03_permission/code.py
# 运行测试
python -m pytest
# 检查代码规范
ruff check s01_agent_loop s02_tool_use s03_permission src tests
```
