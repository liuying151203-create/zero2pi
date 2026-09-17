# s01：核心循环

这一章只实现 Agent 的最小闭环：模型返回 `tool_use` 时执行工具，把 `tool_result` 放回消息历史并继续请求；模型不再调用工具时，循环结束。

```text
用户消息 → 模型 → tool_use → 执行工具 → tool_result → 模型
                         ↑__________________________|
```

## 运行

在项目根目录执行：

```powershell
Copy-Item .env.example .env
# 在 .env 中填写 ANTHROPIC_API_KEY 和 MODEL_ID
python s01_agent_loop/code.py
```

本章直接执行模型生成的 shell 命令，仅用于理解循环机制，请在临时目录中运行。权限控制留到后续章节。

默认单次请求超时 60 秒且不自动重试；可通过 `MODEL_TIMEOUT_SECONDS` 和 `MODEL_MAX_RETRIES` 调整。运行时会显示请求进度，网络或 API 错误会直接打印出来。

## 参考与差异

- `lcc` 使用 `agent_loop(messages)` 配合模块级客户端和工具，突出最小循环。
- 本章保留这个循环，但把模型请求和工具执行作为参数传入，借鉴 `pi` 的职责分离，便于替换和测试。
- 相比 `pi`，本章暂不引入事件流、状态对象、取消信号和完整运行时，只保留核心闭环。
- 当前运行示例使用 Anthropic Messages API；DeepSeek 等 OpenAI 兼容模型需要后续 provider adapter，不能只改 `.env`。
