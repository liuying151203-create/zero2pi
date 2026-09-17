# s02：工具调用

这一章在 s01 的单个 `bash` 工具上增加工具定义、处理函数和分发器，让模型可以直接选择 `read_file`、`write_file`、`edit_file` 和 `glob`，不必把所有操作都翻译成 shell 命令。

## 核心变化

```text
tool_name + arguments → TOOL_HANDLERS → handler(**arguments) → tool_result
```

新增工具只需要两步：

1. 在 `TOOLS` 中声明名称、用途和参数 schema；
2. 在 `TOOL_HANDLERS` 中注册名称到处理函数的映射。

核心循环仍然不变，只把 s01 的固定 `run_bash(command)` 换成通用的 `dispatch(name, arguments)`。

## 运行

在项目根目录执行：

```powershell
python s02_tool_use/code.py
```

示例任务：`读取 README.md`、`查找所有 Python 文件`、`创建一个测试文件再读回来`。

本章的文件工具限制在工作目录内，但 `bash` 仍可执行模型生成的命令；完整权限控制留到后续章节。

## 参考与差异

- `lcc` 使用“工具定义 + handler 注册表”，目的是让新增工具只改注册处，保持 s01 循环稳定。
- 本章沿用这个结构，并采用 `pi` 中工具名、参数、处理函数分离的思路；这样后续可在 s03（权限边界）和 s04（Hooks）中独立加入校验与生命周期控制。
- 本章暂不引入 `pi` 的 schema 校验、事件流和并行执行，因为当前先验证顺序分发；schema 校验将在 s03，事件流和并行执行将在后续运行时章节（编号待定）实现。
