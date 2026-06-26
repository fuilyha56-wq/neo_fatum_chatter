# Neo Fatum Chatter (NFC)

> ## v2.4.1 更新（缓存边界强化版）
>
> **负面行为注入系统提示词**
> - `core.toml` `[personality].negative_behaviors` 现在会作为独立的 `<absolute_prohibitions>` section 注入 SYSTEM payload，与 `safety_guidelines` 形成"正面准则 + 绝对禁令"的双层约束。
> - 空值时 section 完全消失（不留空行），不影响提示词结构。
> - 此字段为静态配置，加入 system prompt 不破坏前缀缓存稳定性。
>
> **context_contributions 双 scope 路径**
> - 激活 `ContextContribution.scope = "session"` 字段：第三方插件可注入 session 级稳定内容，渲染到 `[持久上下文]` 块，与 SYSTEM payload 共同构成可缓存前缀。
> - planner 层对 session 贡献做 hash 缓存：内容不变时跨轮复用，避免重复渲染。
> - turn 级贡献（默认）保持原有行为，每轮末尾临时附加到 `[附加上下文]`。
> - `on_prompt_build` publish 调用预置 `"context_contributions": []` key，handler 可直接追加，无需走 legacy extra 路径。
>
> **缓存命中率验证**
> - 通过 DeepSeek API 实测：system prompt 640 tokens 稳定命中，跨 5 轮不同 user 内容（含 500 字随机垃圾、多 payload、contributions）cache_hit 绝对值零波动。
>
> ---

> ## v2.4.0 更新（架构增强版）
>
> **语音通话历史压缩**
> - 新增 `VoiceCallHistoryHandler`：订阅 `voice_call.ended` 事件，将通话期间的完整对话压缩为 1 对 chain entry（user/assistant），无论通话多长只占 1 个链条槽位，保证挂断后上下文连贯。
>
> **前缀缓存稳定性**
> - 系统提示词构建绕过 `on_prompt_build` 事件，防止第三方插件动态注入破坏 SYSTEM payload 稳定性，最大化 LLM 前缀缓存命中率。
> - 动态上下文（channel/summary/narrative/time）合并到一个 USER payload，SYSTEM payload 仅保留稳定的系统提示词。
> - 新增 `_hot_update_summary`：异步压缩完成后在当前对话循环内热替换 summary，无需等到下次 execute 重建。
>
> **运行时架构清晰化**
> - 新增 `TurnTrigger` 4 值触发枚举（`domain/turn_trigger.py`）：NEW_MESSAGES > FOLLOWUP_TOOL_RESULT > TIMEOUT_EXPIRED > IDLE_WAIT，显式优先级分类取代散落的 if/elif 链。
> - 新增 `unread_policy` 独立模块（`runtime/unread_policy.py`）：集中处理主动触发 vs 真实消息的优先级和打断过滤逻辑。
>
> ---
>
> ## v2.3.2-alpha.1 更新（上下文稳态防御预览版）
>
> **上下文稳态防御**
> - 新增 RequestView 发送视图：本轮临时附加上下文只参与当前请求，不再写回长期 response 链，降低动态上下文污染。
> - 新增按 `ToolCall.id` / `ToolResult.call_id` 配对的 tool_result 自愈，保留合法的 `assistant(tool_calls) -> tool_result -> user`，只清理真正孤立或不匹配的 tool_result。
> - 初始上下文增加构建前控量：可限制恢复进 LLM 的 chain 条数与 fused narrative 字符数，降低主程序 token 裁剪触发概率。
>
> **用户画像工具（内嵌版）**
> - 新增 `nfc_query_activity_pattern`：从数据库查询对方的消息活跃时段分布，了解作息和出没时间。
> - 新增 `nfc_record_habit`：记录对对方习惯的观察（如作息、出没规律、行为偏好），持久化到 session。
> - 新增 `nfc_query_habits`：查询已记录的习惯观察，可按分类过滤。
> - Session 新增 `user_habits` 字段，习惯数据随 session JSON 持久化，最多 50 条。
>
> ## v2.3.2
>
> **感知兜底回填优化**
> - 感知阶段耗尽重试仍无工具调用时，改用 sub actor 从模型纯文本输出中提取可发送的回复内容，替代直接发送原始感知文本。
> - 新增 `perception_extract_task` 配置项（默认 `sub_actor`），可切换为 `actor` 使用主对话模型提取。
> - 提取提示词增强：区分"面向对方的回复"与"内心活动"，宁可少提取也不混入内心独白。
>
> ## v2.3.1
>
> - 修复 NFC 动作组件注册兼容性：`nfc_reply` / `do_nothing` / `schedule_proactive` 均显式声明 `associated_types = ["text"]`，避免新版框架加载时报 `associated_types 必须是非空 list`。
> - 结构性重构（不影响现有配置和外部行为）：`execution/` 层、`domain/session_state.py`、`protocol/call_resolver.py`、`protocol/perception_retry.py`、`handlers/stream_wakeup_adapter.py` 等。
> - VLM / 多模态健壮性：base64 校验、单张失败不中断、`skip_vlm_for_stream` 不存在时降级。
> - 上下文污染修复：timeout 临时 prompt 不再写入 `session.chain_payloads`，`update_chain()` 与 `restore_chain_payloads()` 清理误持久化的 system reminder 等。
>
> ---

> **版本选择说明（重要，置顶）**
>
> 插件市场默认安装最新版本。下表只列出几个有代表性的里程碑版本，**除当前默认推荐版本外，其他旧版本不再维护**，仅作为回退或参考保留。
>
> | 版本 | 定位 | 说明 |
> |------|------|------|
> | `v2.0.2` | baseline 修复版 | 保留原上下文行为，含 manifest 身份修复、系统提示词空值修复、作者信息更新；没有 prefix cache 优化，适合担心缓存优化影响行为的人 |
> | `v2.1.1` | 末代稳定版 | 插件正式更名为 Neo Fatum Chatter；继承 prefix cache 优化（去相对分钟数、冻结融合叙事）、情绪轨迹与活跃时段学习，以及新版主程序 LLM/Wait 恢复协议兼容修复 |
> | `v2.2.2-beta` | 过渡 beta | 主要做 `runtime/orchestrator` 大幅重构与 `compressor` / `interrupt_controller` / `summary_service` 等运行时模块的调整，不含 v2.3.0-beta 的层级抽离与多模态健壮性修复 |
> | `v2.3.2` | 稳定版 | v2.3 正式版：感知兜底回填改用 sub actor 提取、上下文污染修复、NFC Action 注册兼容修复 |
> | `v2.4.0` | 架构增强 | 语音通话历史压缩、前缀缓存稳定性优化、TurnTrigger 显式枚举、unread_policy 独立模块、hot_update_summary 热替换 |
> | `v2.4.1` | **当前稳定推荐** | 缓存边界强化：negative_behaviors 注入系统提示词、context_contributions 双 scope 路径激活、缓存命中率实测验证 |
>
> **维护说明**：当前建议生产环境使用 `v2.4.1`。`v2.0.2` / `v2.1.1` / `v2.2.2-beta` / `v2.3.x` / `v2.4.0` 仅作为历史参考与回退选项保留，**不再接受 bug 修复或兼容性更新**。

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
