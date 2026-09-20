# s05：会话持久化

本章在 s04 的 Agent loop 上增加线性 JSONL 会话：每次默认创建新对话；只有在启动时传入 `--session`，才加载指定历史记录。

## 架构流程

```mermaid
flowchart TD
    A[启动程序] --> B{传入 --session?}
    B -- 否 --> C[创建新的 JSONL 文件]
    B -- 是 --> D[加载指定 JSONL 文件]
    C --> E[SessionManager 恢复 messages]
    D --> E
    E --> F[Agent loop]
    G[自然语言任务] --> H[追加 user 消息]
    H --> I[请求模型]
    I --> J[追加 assistant / tool_result]
    J --> F
```

```text
Message → SessionMessage → JsonlSessionStore → SessionManager → agent_loop
```

## 组件关系与调用链

| 层次 | 组件 | 作用 |
| --- | --- | --- |
| 消息模型 | `SessionMessage` | 在 Agent 消息与 JSONL 记录间转换 |
| 存储层 | `JsonlSessionStore` | 只负责按行追加和顺序读取文件 |
| 管理层 | `SessionManager` | 对外提供打开、加载和追加消息的接口 |
| 启动选择 | `session_path_from_cli()` | 默认创建新文件；只在 `--session` 存在时选择历史文件 |

`main()` 先调用 `session_path_from_cli()`，再用 `SessionManager.open()` 加载历史，并将 `session.append_message` 注入 `agent_loop()`。`_append_message()` 同时更新内存 `messages` 与 JSONL，确保恢复后仍包含工具调用及其结果。

## 运行

```powershell
# 创建新的独立会话；实际文件路径会在启动后打印
python -m s05_session.code

# 加载一个已有的历史会话
python -m s05_session.code --session .sessions\session-20260919-120000.jsonl
```

从项目根目录使用 `-m` 运行，以保证章节之间的兄弟模块可以正常导入。默认会话保存在 `.sessions/`；`--session` 指向的文件必须已经存在。

## 参考与差异

- `lcc` 的 memory 同时处理长期存储、召回、提取与整理，解决跨会话知识复用；本章只保存和恢复原始对话记录，避免把不同职责混在同一层。
- `pi` 的 session 采用追加式记录，并从持久化状态重建上下文，解决运行中断后的连续对话；本章保留这一方向，但使用“默认新建、显式路径恢复”的线性文件模型。s06 在此基础上将完整日志与模型上下文拆为投影关系。
- s04 的 Hooks 只管理工具生命周期；s05 通过保存回调写入消息，使会话持久化保持在 Harness 内部，而不进入系统提示词。
