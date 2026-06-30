# Neo Fatum Chatter (NFC)

> ## v2.5.3 更新
>
> **注入点兼容修复**
> - `[flashback]` 新增 `injection_point` 配置项，默认值 `default_chatter_user_prompt`，对齐 booku_memory / sisp 等主流第三方注入器订阅的事件名，开箱即用。
> - 之前 NFC 写死发布 `NFC_user_prompt` 事件，但市场绝大多数注入器只订阅 `default_chatter_user_prompt`，导致第三方快照/上下文无法注入。
> - 用户如需保留旧名，可在 `config.toml` 显式设置 `[flashback] injection_point = "NFC_user_prompt"` 回退。
> - `plan_user_turn` / `build_user_payload` 现在接收 `config` 参数读取注入点名，调用方已同步传入。
>
> **梦话问题修复**
> - 修复主动发起触发消息未设置 `sender_role` 字段的问题。之前 `ProactiveHandler._build_proactive_message` 构造的 `Message` 缺少 role 标记，被 `format_message_line` 渲染为普通用户消息，模型会把系统触发误解为"用户在说话"，进而脑补出一条用户回复。
> - 触发消息现在显式带 `sender_role="system"`，渲染为 `<system>` 前缀，模型可一眼区分系统触发与真实用户消息。
>
> **关闭 NFC 后干扰其他 Chatter 修复**
> - 修复 `general.enabled=false` 后 NFC Chatter 仍留在 `ChatterManager._active_chatters` 中、导致 DFC 等其他 chatter 无法接管的问题。
> - `on_config_updated` 在切换为 disabled 时，除了 `registry.unregister` 还会遍历 `_active_chatters` 清除所有 `chatter_name == "neo_fatum_chatter"` 的实例，并通过 `restart_stream_loop` 让受影响的流重新走 chatter 选择流程。
>
> ---

> ## v2.5.2 更新
>
> **摘要开关**
> - `[prompt]` 新增 `summary_enabled` 配置项（默认 `true`）。
> - 关闭后不再触发近期记忆摘要压缩任务，也不再向提示词注入 `history_summary`。
> - session 中已存在的旧摘要不会被清除，但不会再出现在上下文中，可随时重新开启恢复使用。
>
> **等待抑制期显式缓冲**
> - 修复 `wait.suppress_early_wake=true` 时，抑制期间每条新消息都会唤醒主循环、重复进入 `prepare_turn_input` 抑制分支并产生大量日志的问题。
> - 抑制期间到达的新消息现在被显式收集到 `session.suppressed_messages` 缓冲区（按 `message_id` 去重）。
> - 等待超时到期后，缓冲区中的所有消息会**一次性合并为单条 USER payload** 注入 LLM，整个抑制期间只会构建一次上下文。
> - 超时点改为以 `waiting_config.started_at + max_wait_seconds` 为基准的绝对截止时间，不再因为新消息到达而不断推迟。
>
> ---
>
> **版本选择说明（重要，置顶）**
>
> 插件市场默认安装最新版本。**除当前默认推荐版本外，其他旧版本不再维护**，仅作为回退或参考保留。
>
> | 版本 | 说明 |
> |------|------|
> | `v2.5.3` | **当前稳定推荐** — 注入点兼容 + 梦话修复 + 关闭干扰修复 |
> | `v2.5.2` | 摘要开关 + 等待抑制期显式缓冲 |
> | `v2.5.1` | 工具调用协议强化 |
> | `v2.4.1` | 缓存边界强化 |
> | `v2.3.2` | 感知兜底改用 sub actor 提取 |
> | `v2.1.1` | 更名为 NeoFatumChatter，prefix cache 优化 |
> | `v2.0.2` | baseline 修复版 |
>
> **维护说明**：生产环境建议使用 `v2.5.3`。旧版本仅作为回退选项保留，**不再接受 bug 修复或兼容性更新**。

*Fatum — 拉丁语中"命运"的意思。*

**Neo Fatum Chatter 对话引擎 — 基于心理活动流的私聊特化聊天器** — Neo-MoFox 插件

---

## 概述

NFC 是一个面向私聊场景的 Chatter 插件，核心设计是将 LLM 的每次决策与内心独白（MentalLog）绑定，形成连续的心理活动流。对话历史与内心活动按时间线交织，让模型在回复时不仅能看到说了什么，还能"回想起"当时在想什么。

**主要能力**

- 每次回复附带内心独白，记录当前情绪与期待
- 显式维护 SceneState，避免把平台/私聊通道自动脑补成生活场景
- 等待超时后分析消息类型，决定追问、继续等或结束
- 沉默超过阈值后有概率主动发起对话，深夜自动静默
- 多条连发消息在积累窗口内合并后统一处理
- LLM 生成期间若检测到新消息，取消当前请求并重新处理
- 原生多模态支持，图片直接进 LLM 上下文
- 回复拆分为短句模拟打字节奏逐条发送，可选流式打字机效果
- 主动发起以预约（`schedule_proactive`）为主、沉默触发为兜底

---

## 架构

### 分层

| 层 | 目录 | 职责 |
|---|---|---|
| 入口 / 配置 | `plugin.py`, `config.py`, `manifest.json` | 注册组件、加载配置、调度器 hook |
| 运行时编排 | `runtime/` | 主循环 `orchestrator`、回合准备与提交 `turn_controller`、消息积累 `message_buffer`、LLM 打断 `interrupt_controller`、`unread_policy` 消息优先级策略 |
| 协议归一化 | `protocol/` | `response_normalizer` / `compat_adapter`（含 DeepSeek 兼容）/ `decision_parser` / `call_resolver`（工具调用名解析）/ `perception_retry`（感知阶段未发送草稿重写策略） |
| 执行 | `execution/` | `reply_executor` 段落清洗与分段发送，`ExecutionResult` 数据类 |
| 上下文 | `context/` + `prompts/` | `planner`/`renderer` + 多 `sources/` 提供分源上下文；`prompts/` 内含 `builder` / `modules` / `templates` |
| 服务 | `services/` | `timeout_service` / `proactive_service` / `summary_service` / `context_sanitizer` / `multimodal_service` / `perception_extractor` / `compressor` |
| 领域 | `domain/` | `NFCSession`、`Decision`、`SceneState`、`TurnTrigger` 等纯状态模型 |
| 持久化 | `persistence/` | `session_store` 文件与索引 IO、并发锁 |
| 动作 | `actions/` | LLM 工具调用 schema 壳：`nfc_reply` / `do_nothing` / `schedule_proactive` |
| 事件入口 | `handlers/` | `proactive_handler` 主动发起事件入口 + `voice_call_history_handler` 语音通话历史压缩 + `stream_wakeup_adapter` 隔离框架私有 API |
| 调试 | `debug/` | `log_formatter` 提示词 / 响应美化输出 |
| 测试 | `tests/` | pytest 骨架与首批协议/执行/多模态测试（5 文件 / 45 例） |

> 兼容入口：`session.py` 仅作为兼容 re-export，把 `NFCSession` / `NFCSessionStore` 转发到 `domain/` 与 `persistence/`，新代码请直接从子包引用。

### 单一决策协议

NFC 当前正式只保留一条内部协议：

```
tool calling -> response_normalizer -> Decision
```

运行时主流程从 `chatter.py` 抽离到 `runtime/orchestrator.py`，`NeoFatumChatter` 主要保留门面与 helper。回合准备与提交下沉到 `runtime/turn_controller.py`，消息积累窗口与 LLM 打断控制分别收敛在 `runtime/message_buffer.py`、`runtime/interrupt_controller.py`。超时、主动预约、近期摘要、多模态历史图片注入等外围副作用通过 `services/` 目录进入主流程。

核心动作如下：

| 动作 | 用途 |
|------|------|
| `nfc_reply` | 发送消息，携带 `content`、`thought`、`expected_reaction`、`max_wait_seconds`、`mood` |
| `do_nothing` | 选择不回复，携带 `thought`、`max_wait_seconds` |
| `schedule_proactive` | 预约下一次主动思考时间，携带 `delay_minutes`、`reason` |

用户画像工具（内嵌版）：

| 动作 | 用途 |
|------|------|
| `nfc_query_activity_pattern` | 从数据库查询对方消息活跃时段分布，了解作息和出没时间 |
| `nfc_record_habit` | 记录对对方习惯的观察（如作息、出没规律、行为偏好），持久化到 session |
| `nfc_query_habits` | 查询已记录的习惯观察，可按分类过滤 |

### 消息积累窗口

检测到第一条新消息后，NFC 等待一个固定窗口（默认 1.5 秒）以收集连发的多条消息，再统一提交给 LLM。最大积累时长默认为 5 秒，防止无限延迟。

### 等待抑制与显式缓冲

当 `wait.suppress_early_wake=true` 时，NFC 在等待回复期间收到新消息不会立即处理，而是把它们显式收集到 session 内的 `suppressed_messages` 缓冲区。等待超时到期后，缓冲区里的所有消息会**一次性合并为单条 USER payload** 注入 LLM，整个抑制期间只会构建一次上下文，避免每条新消息都触发一次 prompt 构建。

配合 `general.enable_custom_tick_interval=true` 使用时，等待期间的 tick 唤醒只会做轻量的消息收集，超时点以 `waiting_config.started_at + max_wait_seconds` 为准（绝对截止时间），不会被新到达的消息不断推迟。

### LLM 生成打断

LLM 生成期间，每 0.5 秒检测一次是否有新消息到达。检测到打断时会取消当前请求，重新积累新消息并重新提交。

### 超时与主动发起

回复后可以进入等待状态；等待超时后会重新注入上下文，让 LLM 决策追问、放弃或继续等待。长时间沉默时，主动发起检查会按配置决定是否触发新对话。`schedule_proactive` 是主要的主动联系手段，沉默触发器仅作为兜底。

---

## 配置

配置文件路径：`config/plugins/neo_fatum_chatter/config.toml`

### `[general]`：基础与模型

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用 |
| `model_task` | `actor` | 走 `model.toml` 的 task；`models` 为空时使用 |
| `models` | `[]` | 模型名列表，非空时覆盖 `model_task`，按顺序 fallback |
| `temperature` | `0.7` | 模型温度，仅在 `models` 非空时生效 |
| `max_tokens` | `8000` | 最大输出 token，仅在 `models` 非空时生效 |
| `native_multimodal` | `false` | 启用后图片直接进 LLM payload |
| `max_images_per_payload` | `4` | 原生多模态总图片配额（bot 已发 > 用户新消息 > 历史补充） |
| `use_tool_calling` | `true` | 主动发起 / 超时上下文是否使用工具调用决策提示 |
| `max_compat_retries` | `1` | 纯文本草稿未形成工具调用时的重试次数 |
| `perception_extract_task` | `sub_actor` | 感知兜底回填时的模型任务名；`sub_actor` 用轻量模型，`actor` 用主模型 |
| `max_consecutive_llm_failures` | `15` | 连续 LLM 失败容忍次数，0 表示不限制 |
| `custom_decision_prompt` | `""` | 注入到系统提示词安全准则之后的自定义指导，留空不生效 |
| `blocked_tools` | `["send_text","pass_and_wait","stop_conversation"]` | 屏蔽不暴露给 LLM 的工具末段名 |
| `segment_instruction` | 默认分段指引 | 注入到提示词的分段指令，留空不注入 |
| `wait_instruction` | 默认等待指引 | 注入到提示词的 `max_wait_seconds` 说明，留空不注入 |
| `enable_custom_tick_interval` | `false` | 是否启用 NFC 独立 tick 间隔；关闭跟随主程序 `bot.tick_interval` |
| `custom_tick_interval` | `5.0` | 启用上一项时使用的 tick 间隔（秒，必须 > 0） |

### `[wait]`：等待机制

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用回复等待 |
| `min_seconds` | `10.0` | 最小等待秒数 |
| `max_seconds` | `600.0` | 最大等待秒数 |
| `max_consecutive_timeouts` | `3` | 连续超时上限，达到后不再等待 |

### `[proactive]`：主动发起

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用主动发起 |
| `silence_threshold` | `7200` | 沉默阈值（秒） |
| `trigger_probability` | `0.3` | 沉默触发概率 |
| `min_interval` | `1800` | 两次主动发起最小间隔（秒） |
| `quiet_hours_start` / `quiet_hours_end` | `23:00` / `07:00` | 勿扰时段 |
| `check_interval` | `60` | 主动发起检查间隔（秒） |
| `schedule_guidance` | 默认指引 | `schedule_proactive` 工具描述中的使用场景指导 |

### `[reply]`：回复节奏

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `typing_chars_per_sec` | `15.0` | 模拟打字速度（字/秒） |
| `typing_delay_min` / `typing_delay_max` | `0.8` / `4.0` | 打字延迟范围（秒） |
| `segment_delay_min` / `segment_delay_max` | `0.5` / `2.0` | 多段消息间隔范围（秒） |
| `streaming_enabled` | `false` | 是否启用流式回复（打字机效果，需平台适配器支持编辑消息） |
| `streaming_chunk_size` | `10` | 流式回复每次追加字符数 |
| `streaming_interval` | `0.1` | 流式回复每次追加间隔（秒） |

### `[prompt]`：活动流与压缩

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `summary_enabled` | `true` | 是否启用近期记忆摘要。关闭后不再触发摘要压缩，也不再向提示词注入 `history_summary`；session 中已存在的旧摘要不会被清除，但不会再出现在上下文中 |
| `max_log_entries` | `50` | 最大活动流条目数 |
| `max_context_payloads` | `20` | LLM 上下文持久化链最大条目数（超出裁剪最旧 USER/ASSISTANT 对） |
| `max_initial_chain_payloads` | `12` | execute 启动时最多恢复进 LLM 的持久化 chain 条数，不影响持久化保留数量 |
| `max_fused_narrative_chars` | `12000` | 融合叙事最大字符数，超出时仅保留最近部分，降低框架 token 裁剪触发概率 |
| `compress_every_n_rounds` | `50` | 每完成 N 轮触发一次近期记忆压缩 |
| `compress_days_window` | `3.0` | 压缩覆盖的历史时间窗口（天） |
| `min_compress_interval_minutes` | `120.0` | 两次压缩最短间隔（分钟） |

### `[buffer]`：消息积累与打断

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `accumulate_window` | `1.5` | 消息积累窗口（秒），0 禁用 |
| `accumulate_max_window` | `5.0` | 积累窗口最大总时长（秒） |
| `interrupt_enabled` | `true` | 是否启用 LLM 生成打断 |
| `interrupt_poll_seconds` | `0.5` | 打断检测轮询间隔（秒） |

### `[debug]`：调试

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `show_prompt` | `false` | 日志中显示完整提示词 |
| `show_response` | `true` | 日志中显示 LLM 响应美化摘要 |

---

## 安装

通过 Neo-MoFox 插件市场安装：

```bash
mpdt market install neo_fatum_chatter
```

或从 GitHub Release 手动下载指定版本 `.mfp` 文件放入 `plugins/` 目录。

---

## 开发

测试（首批协议/执行/多模态层骨架）：

```bash
cd plugins/neo_fatum_chatter
pytest tests/ -c pyproject.toml
```

---

## 许可证

AGPL-3.0
