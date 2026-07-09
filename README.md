# Neo Fatum Chatter (NFC)

> ## v2.5.6 更新
>
> - **可配置流式输出**：接入外部流式 Service（支持 `start_streaming`），对 QQBot 私聊（C2C）做打字机式逐块更新；非 qqbot 或启动失败时降级普通发送。新增 `[reply].streaming_service_signature` 指定流式 Service 签名，留空自动发现。流式更新中途失败时自动 `controller.end()` 收尾，避免流卡死与重复消息。
>
> ---
>
> **版本选择说明**
>
> | 版本 | 说明 |
> |------|------|
> | `v2.5.6` | **当前推荐** — 可配置流式输出（QQBot C2C 打字机）|
> | `v2.5.5` | 系统提示词可自定义 + 人设沉浸强化 |
> | `v2.5.4` | enabled 动态切换 + 注入点可配置 + v2.5.3 三项修复 |
> | `v2.5.3` | 打断装哑 / tool_call 续轮丢失 / 主动思考炸缓存 |
> | `v2.5.2` | 摘要开关 + 等待抑制期显式缓冲 |
> | `v2.5.1` | 工具调用协议强化 |
> | `v2.4.1` | 缓存边界强化 |
> | `v2.3.2` | 感知兜底改用 sub actor 提取 |

*Fatum — 拉丁语中"命运"。*

**Neo Fatum Chatter 对话引擎 — 基于心理活动流的私聊特化聊天器** — Neo-MoFox 插件

---

## 概述

NFC 是面向私聊场景的 Chatter 插件。核心设计是把 LLM 的每次决策与内心独白（MentalLog）绑定，形成连续的心理活动流。对话历史与内心活动按时间线交织，让模型回复时不仅看到说了什么，还能"回想起"当时在想什么。

**主要能力**

- 每次回复附带内心独白，记录情绪与期待
- 显式维护 SceneState，避免把平台/私聊通道脑补成生活场景
- 等待超时后分析消息类型，决定追问、继续等或结束
- 沉默超过阈值有概率主动发起，深夜自动静默
- 多条连发消息在积累窗口内合并后统一处理
- LLM 生成期间检测到新消息会取消当前请求并重新处理
- 原生多模态支持，图片直接进 LLM 上下文
- 回复拆分为短句模拟打字节奏逐条发送，可选流式打字机
- 主动发起以 `schedule_proactive` 预约为主、沉默触发为兜底

---

## 架构

### 目录结构

| 层 | 目录 / 文件 | 职责 |
|---|---|---|
| 入口 / 配置 | `plugin.py` `config.py` `manifest.json` | 注册组件、加载配置、调度器 hook、`enabled` 动态注册/注销 |
| Chatter 门面 | `chatter.py` | `NeoFatumChatter` 入口，委托 runtime orchestrator，保留 helper |
| 运行时编排 | `runtime/` | `orchestrator` 主循环、`turn_controller` 回合准备与提交、`message_buffer` 消息积累、`interrupt_controller` LLM 打断、`request_view` 请求视图裁剪、`unread_policy` 未读优先级 |
| 协议归一化 | `protocol/` | `response_normalizer` `compat_adapter`（含 DeepSeek 兼容）`decision_parser` `call_resolver` `perception_retry` |
| 执行 | `execution/` | `reply_executor` 段落清洗与分段发送，`ExecutionResult` 数据类 |
| 上下文 | `context/` + `prompts/` | `planner` `renderer` + 多 `sources/`；`prompts/` 含 `builder` `modules` `templates` |
| 服务 | `services/` | `timeout_service` `proactive_service` `summary_service` `context_sanitizer` `multimodal_service` `perception_extractor` `compressor` |
| 思考器 | `thinker/` | `proactive` 主动发起检查、`timeout_handler` 超时触发 |
| 领域 | `domain/` | `NFCSession` `Decision` `SceneState` `TurnTrigger` 纯状态模型 |
| 持久化 | `persistence/` | `session_store` 文件与索引 IO、并发锁 |
| 动作 | `actions/` | LLM 工具 schema 壳：`nfc_reply` `do_nothing` `schedule_proactive` `query_activity_pattern` `record_habit` `query_habits` |
| 事件入口 | `handlers/` | `proactive_handler` 主动发起事件入口、`voice_call_history_handler` 语音通话历史压缩、`stream_wakeup_adapter` 隔离框架私有 API |
| 多模态 | `multimodal.py` | 图片预算、媒体提取与混合内容构建 |
| 兼容 / 解析 | `llm_compat.py` `parser.py` `mental_log.py` `models.py` | LLM 兼容层、调用解析、活动流日志、枚举与数据模型 |
| 调试 | `debug/` | `log_formatter` 提示词与响应美化输出 |
| 测试 | `tests/` | pytest 协议 / 执行 / 多模态 / 配置 / 运行时去重 |
| 兼容入口 | `session.py` | `NFCSession` / `NFCSessionStore` re-export 到 `domain/` `persistence/`，新代码请直接从子包引用 |

接口清单见 [API.md](API.md)。

### 单一决策协议

NFC 当前正式只保留一条内部协议：

```
tool calling -> response_normalizer -> Decision
```

运行时主流程在 `runtime/orchestrator.py`，回合准备与提交在 `runtime/turn_controller.py`。超时、主动预约、近期摘要、多模态历史图片注入等外围副作用通过 `services/` 进入主流程。

**核心动作**

| 动作 | 用途 |
|------|------|
| `nfc_reply` | 发送消息，携带 `content` `thought` `expected_reaction` `max_wait_seconds` `mood` |
| `do_nothing` | 不回复，携带 `thought` `max_wait_seconds` |
| `schedule_proactive` | 预约下一次主动思考，携带 `delay_minutes` `reason` |

**用户画像工具**

| 动作 | 用途 |
|------|------|
| `nfc_query_activity_pattern` | 查询对方消息活跃时段分布 |
| `nfc_record_habit` | 记录对对方习惯的观察，持久化到 session |
| `nfc_query_habits` | 查询已记录的习惯观察，可按分类过滤 |

### 消息积累窗口

检测到第一条新消息后，NFC 等待一个固定窗口（默认 1.5 秒）收集连发的多条消息，再统一提交给 LLM。最大积累时长默认 5 秒。

### 等待抑制与显式缓冲

`wait.suppress_early_wake=true` 时，等待期间收到的新消息不会立即处理，而是收集到 `session.suppressed_messages`。超时到期后缓冲区里的所有消息**一次性合并为单条 USER payload** 注入 LLM，整个抑制期只构建一次上下文。超时点以 `waiting_config.started_at + max_wait_seconds` 为绝对截止时间，不被新消息推迟。

### LLM 生成打断

LLM 生成期间每 `interrupt_poll_seconds`（默认 0.5 秒）检测一次新消息。检测到打断时取消当前请求，把打断消息写入 `mental_log`，清掉 `waiting` 状态，重新进入主循环。

### 超时与主动发起

回复后进入等待状态；超时后重新注入上下文，让 LLM 决策追问、放弃或继续等待。长时间沉默时主动发起检查按配置决定是否触发。`schedule_proactive` 是主要主动联系手段，沉默触发器仅作兜底。主动思考触发的富上下文（沉默时长 / 近期活动 / 预约理由）作为 turn contribution 临时注入，不进入持久历史，保护 prompt prefix cache。

### enabled 动态切换

`[general].enabled=false` 时：
- `get_components()` 不返回 `NeoFatumChatter`，重启后 chatter_manager 选不到 NFC
- `on_config_updated()` 注销已注册的 Chatter，清理所有运行中的 NFC active chatter 实例，并强制重启受影响的流循环，让 `ChatterManager` 重新选 chatter（DFC 等才能接管）

其他组件（actions / handlers）始终注册，满足"插件可被发现和加载，但不参与 chatter 调度"的语义。

---

## 配置

配置文件路径：`config/plugins/neo_fatum_chatter/config.toml`

### `[general]` 基础与模型

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用 NFC Chatter（false 时动态让位） |
| `model_task` | `actor` | 走 `model.toml` 的 task；`models` 为空时使用 |
| `models` | `[]` | 模型名列表，非空时覆盖 `model_task`，按顺序 fallback |
| `temperature` | `0.7` | 模型温度，仅在 `models` 非空时生效 |
| `max_tokens` | `8000` | 最大输出 token，仅在 `models` 非空时生效 |
| `native_multimodal` | `false` | 启用后图片直接进 LLM payload |
| `max_images_per_payload` | `4` | 原生多模态总图片配额（bot 已发 > 用户新消息 > 历史补充） |
| `use_tool_calling` | `true` | 主动发起 / 超时上下文是否使用工具调用决策提示 |
| `max_compat_retries` | `1` | 纯文本草稿未形成工具调用时的重试次数 |
| `perception_extract_task` | `sub_actor` | 感知兜底回填时的模型任务名 |
| `max_consecutive_llm_failures` | `15` | 连续 LLM 失败容忍次数，0 表示不限制 |
| `custom_decision_prompt` | `""` | 注入到系统提示词的自定义指导，留空不生效 |
| `blocked_tools` | `["send_text","pass_and_wait","stop_conversation"]` | 屏蔽不暴露给 LLM 的工具末段名 |
| `segment_instruction` | 默认分段指引 | 注入提示词的分段指令，留空不注入 |
| `wait_instruction` | 默认等待指引 | 注入提示词的 `max_wait_seconds` 说明，留空不注入 |
| `enable_custom_tick_interval` | `false` | 是否启用 NFC 独立 tick 间隔 |
| `custom_tick_interval` | `5.0` | 启用上一项时使用的 tick 间隔（秒） |

### `[wait]` 等待机制

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用回复等待 |
| `min_seconds` | `10.0` | 最小等待秒数 |
| `max_seconds` | `600.0` | 最大等待秒数 |
| `max_consecutive_timeouts` | `3` | 连续超时上限 |
| `suppress_early_wake` | `true` | 等待期间收到新消息是否抑制到超时点合并处理 |

### `[proactive]` 主动发起

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用 |
| `silence_threshold` | `7200` | 沉默阈值（秒） |
| `trigger_probability` | `0.3` | 沉默触发概率 |
| `min_interval` | `1800` | 两次主动发起最小间隔（秒） |
| `quiet_hours_start` / `quiet_hours_end` | `23:00` / `07:00` | 勿扰时段 |
| `check_interval` | `60` | 检查间隔（秒） |
| `activity_service_signature` | `""` | 外部活跃度服务签名，留空走内置判断 |
| `activity_service_method` | `is_user_active` | 外部活跃度服务方法名 |
| `schedule_guidance` | 默认指引 | `schedule_proactive` 工具描述中的使用场景指导 |

### `[reply]` 回复节奏

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `typing_chars_per_sec` | `15.0` | 模拟打字速度（字/秒） |
| `typing_delay_min` / `typing_delay_max` | `0.8` / `4.0` | 打字延迟范围（秒） |
| `segment_delay_min` / `segment_delay_max` | `0.5` / `2.0` | 多段消息间隔范围（秒） |
| `streaming_enabled` | `false` | 是否启用流式打字机效果 |
| `streaming_service_signature` | `""` | 指定流式 Service 签名；留空自动发现支持 `start_streaming` 的 Service |
| `streaming_chunk_size` | `10` | 流式每次追加字符数 |
| `streaming_interval` | `0.1` | 流式追加间隔（秒） |

### `[prompt]` 活动流与压缩

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `summary_enabled` | `true` | 是否启用近期记忆摘要 |
| `max_log_entries` | `50` | 最大活动流条目数 |
| `max_context_payloads` | `20` | LLM 上下文持久化链最大条目数 |
| `max_initial_chain_payloads` | `12` | execute 启动时最多恢复进 LLM 的 chain 条数 |
| `max_fused_narrative_chars` | `12000` | 融合叙事最大字符数 |
| `compress_every_n_rounds` | `50` | 每完成 N 轮触发一次压缩 |
| `compress_days_window` | `3.0` | 压缩覆盖的历史时间窗口（天） |
| `min_compress_interval_minutes` | `120.0` | 两次压缩最短间隔（分钟） |

### `[buffer]` 消息积累与打断

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `accumulate_window` | `1.5` | 消息积累窗口（秒），0 禁用 |
| `accumulate_max_window` | `5.0` | 积累窗口最大总时长（秒） |
| `interrupt_enabled` | `true` | 是否启用 LLM 生成打断 |
| `interrupt_poll_seconds` | `0.5` | 打断检测轮询间隔（秒） |

### `[flashback]` 注入点兼容

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `injection_point` | `default_chatter_user_prompt` | `on_prompt_build` 事件注入点名。默认对齐 booku_memory / DFC bridge 等主流注入器；需回退 NFC 私有注入点时改为 `NFC_user_prompt` |

### `[debug]` 调试

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `show_prompt` | `false` | 日志中显示完整提示词 |
| `show_response` | `true` | 日志中显示 LLM 响应美化摘要 |

---

## 安装

通过 Neo-MoFox 插件市场：

```bash
mpdt market install neo_fatum_chatter
```

或从 GitHub Release 下载 `.mfp` 放入 `plugins/`。

---

## 开发

```bash
cd plugins/neo_fatum_chatter
pytest tests/ -c pyproject.toml
```

接口清单见 [API.md](API.md)。

---

## 许可证

AGPL-3.0
