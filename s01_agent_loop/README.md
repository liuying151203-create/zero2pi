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
python -m s01_agent_loop.code
```

本章直接执行模型生成的 shell 命令，仅用于理解循环机制，请在临时目录中运行。权限控制留到后续章节。

默认单次请求超时 60 秒且不自动重试；可通过 `MODEL_TIMEOUT_SECONDS` 和 `MODEL_MAX_RETRIES` 调整。运行时会显示请求进度，网络或 API 错误会直接打印出来。

终端输出会区分用户、模型、工具、结果和 Agent 回答；颜色仅用于交互展示，不参与核心循环。

系统提示词会先区分“对话请求”和“工作区任务”：问候语直接回答，只有确实需要项目内容时才调用工具，并要求使用最少工具后停止。

## 参考与差异

- `lcc` 使用 `agent_loop(messages)` 配合模块级客户端和工具，目的是用最少代码突出循环本身。
- 本章把模型请求和工具执行作为参数传入，借鉴 `pi` 的职责分离；这样便于替换模型、编写测试，也为后续 provider adapter 章节（编号待定）留出接口。
- 本章暂不引入 `pi` 的事件流、状态对象和取消信号，因为当前只验证核心闭环；这些能力将在后续运行时章节（编号待定）逐项落地。
- 当前运行示例使用 Anthropic Messages API；DeepSeek 等 OpenAI 兼容模型需要后续 provider adapter，不能只改 `.env`。
