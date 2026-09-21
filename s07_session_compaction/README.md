# s07：会话系统 · 上下文压缩

s06 将完整日志投影为模型上下文；s07 在每次模型请求前检查该上下文的字符数。超过阈值时，较早消息会被总结为 `CompactionEntry`，最近消息仍原样保留。

## 架构流程

```mermaid
flowchart LR
    A[完整 JSONL 日志] --> B[build_session_context]
    B --> C{超过字符阈值?}
    C -- 否 --> D[请求模型]
    C -- 是 --> E[总结较早消息]
    E --> F{获得最终文本?}
    F -- 是 --> G[追加模型摘要]
    F -- 否 --> H[重试一次，再写入安全回退摘要]
    G --> I[追加 CompactionEntry]
    H --> I
    I --> B
```

```text
MessageEntry ... → CompactionEntry(summary + retained_tail) → active_context → 模型请求
```

## 组件关系

| 组件 | 作用 | 调用关系 |
| --- | --- | --- |
| `CompactionPolicy` | 定义字符阈值和保留消息数 | 供 `ContextCompactor` 判断 |
| `ContextCompactor` | 分割上下文、调用摘要函数、写入压缩记录 | 请求前由 `CompactedContextRequester` 调用 |
| `CompactionEntry` | 保存摘要与最近消息 | 被 `SessionManager` 追加到 JSONL |
| `build_session_context()` | 只投影最新压缩点及其后的消息 | 每次请求前重新运行 |
| `CompactedContextRequester` | 在一次模型请求前执行压缩并注入最新上下文 | 由入口显式组装会话依赖 |
| `agent_loop()` | 展示完整的模型与工具循环 | 行为继承 s06，在 s07 文件中展开并调用压缩请求组件 |

压缩不会删除旧的 `MessageEntry`。读取时只从最后一个 `CompactionEntry` 开始：先放入摘要和它的 `retained_tail`，再追加该压缩点之后的新消息。因此 JSONL 保留完整事实，模型上下文保持较小。

保留尾部时，若边界正好落在 `assistant(tool_use)` 与 `user(tool_result)` 之间，s07 会把这对消息一起保留，避免破坏模型工具调用协议。

`main()` 先选择会话路径、打开会话，再以具名参数组装组件：`ContextSummarizer` 保存摘要请求所需的模型请求器与额度，`ContextCompactor` 负责压缩决策和落盘。入口将 `CompactedContextRequester` 传给本章展开的 `agent_loop()`，由循环在需要模型响应时调用它。

正常请求顺序为：`compact_if_needed()` 检查并按需保存压缩结果 → `session.build_context()` 重建上下文 → 替换本次请求的 `messages` → 请求模型。摘要生成直接调用底层请求器，避免再次触发压缩。投影函数从日志末尾向前寻找最新压缩点，再依次追加摘要、保留尾部和新消息。

## 运行

```powershell
# 创建 s07 专属会话；超过默认 24000 字符时自动压缩
python -m s07_session_compaction.code

# 加载已有 s07 会话
python -m s07_session_compaction.code --session .sessions\s07\session-20260919-120000.jsonl
```

`.env` 中可调整当前章节实际使用的参数：

```ini
SESSION_COMPACTION_MAX_CHARS=24000
SESSION_COMPACTION_KEEP_RECENT_MESSAGES=6
SESSION_COMPACTION_SUMMARY_MAX_TOKENS=1024
```

`SESSION_COMPACTION_MAX_CHARS` 使用字符数近似上下文大小，便于直接观察和测试；它不是精确 token 计数。

若摘要响应只有 `thinking`、没有最终文本，s07 会自动以至少 1024 token 重试一次。仍未取得文本时，会写入不编造事实的回退摘要，保留最近消息并标出完整 JSONL 文件路径，Agent 不会因此中断。

## 参考与差异

- Pi 将压缩结果作为会话 Entry 保存，并在构建上下文时只使用最后一次压缩点及其后的记录。s07 保留这个边界，使“完整日志”和“模型上下文”各自职责明确。
- lcc 使用工具输出持久化、裁剪、微压缩和手动压缩等多层管线。本章只实现会话摘要这一条自动路径，避免在同一章节混入多种压缩策略。
