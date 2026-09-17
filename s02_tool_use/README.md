# s02：工具调用

这一章在 s01 的单个 `bash` 工具上增加工具定义、处理函数和分发器，让模型可以直接选择 `read_file`、`write_file`、`edit_file` 和 `glob`，不必把所有操作都翻译成 shell 命令。

## 核心变化

```text
tool_name + arguments → TOOL_HANDLERS → handler(**arguments) → tool_result
```

## 组件关系与调用链

本章没有新增类，核心职责由三个数据/函数层次组成：

| 组件 | 作用 |
| --- | --- |
| `TOOLS` | 告诉模型有哪些工具以及参数格式；只描述协议，不执行工具 |
| `TOOL_HANDLERS` | 保存工具名称到 Python 处理函数的映射 |
| `dispatch_tool()` | 根据名称查找 handler，统一捕获参数和运行错误 |
| `agent_loop()` | 请求模型、提取 `tool_use`、调用分发器并回传 `tool_result` |
| `run_read()` / `run_write()` / `run_edit()` / `run_glob()` | 各自负责一种具体文件操作 |

调用关系是：

```text
agent_loop
    ↓
dispatch_tool(name, arguments)
    ↓
TOOL_HANDLERS[name]
    ↓
具体 run_* 方法
    ↓
tool_result
```

这样拆分的原因是：核心循环只负责 Agent 协议，分发器只负责查找和错误边界，具体方法只负责工具本身。新增工具时主要增加定义、处理函数和注册映射，不需要改动循环。

新增工具只需要两步：

1. 在 `TOOLS` 中声明名称、用途和参数 schema；
2. 在 `TOOL_HANDLERS` 中注册名称到处理函数的映射。

核心循环仍然不变，只把 s01 的固定 `run_bash(command)` 换成通用的 `dispatch(name, arguments)`。

模型之所以能返回 `tool_use`、工具名和参数，不是因为系统提示词手写了响应格式，而是因为请求中的 `tools` 参数遵循模型服务商的工具调用协议。模型服务根据工具定义生成结构化响应，代码只负责解析、执行并回传 `tool_result`。

## 运行

在项目根目录执行：

```powershell
python s02_tool_use/code.py
```

示例任务：`读取 README.md`、`查找所有 Python 文件`、`创建一个测试文件再读回来`。

本章的文件工具限制在工作目录内，但 `bash` 仍可执行模型生成的命令；完整权限控制留到后续章节。

终端会用不同标签显示工具名、参数和结果摘要，便于观察多个工具调用的顺序。

系统提示词要求优先使用专用工具、避免主动扫描工作区，并在信息足够时停止；在 Windows 下，`bash` 工具实际执行 `cmd.exe` 命令。

## 参考与差异

- `lcc` 使用“工具定义 + handler 注册表”，目的是让新增工具只改注册处，保持 s01 循环稳定。
- 本章沿用这个结构，并采用 `pi` 中工具名、参数、处理函数分离的思路；这样后续可在 s03（权限边界）和 s04（Hooks）中独立加入校验与生命周期控制。
- 本章暂不引入 `pi` 的 schema 校验、事件流和并行执行，因为当前先验证顺序分发；s03 先实现工具权限，schema 校验、事件流和并行执行将在后续运行时章节（编号待定）实现。
