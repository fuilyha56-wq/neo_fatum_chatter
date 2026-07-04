# NFC 内置 API / 接口入口

> 面向二次开发与外部插件的接口清单。所有路径相对于插件根目录 `neo_fatum_chatter/`。

NFC 的对外接口分为五类：**组件入口**（Chatter / Action / EventHandler）、**事件名**（EventBus 通道）、**配置项**（`config.toml`）、**钩子方法**（运行时回调）、**内部模块函数**（开发调试用）。

---

## 1. 组件入口

`manifest.json` 注册的全部组件，外部可通过 `BasePlugin.get_components()` 或框架注册表获取。

### 1.1 Chatter

| 类 | component_name | 入口文件 | 说明 |
|---|---|---|---|
| `NeoFatumChatter` | `neo_fatum_chatter` | `chatter.py:52` | 心理活动流聊天器，私聊场景 |

`NeoFatumChatter` 由 `BaseChatter` 派生。关键方法：

| 方法 | 位置 | 用途 |
|---|---|---|
| `execute()` | `chatter.py:186` | 异步生成器，输出 `Wait / Success / Failure / Stop` 信号 |
| `apply_stream_runtime_options(chat_stream)` | `chatter.py:72` | 覆盖 tick 间隔 / message buffer 开关 |
| `modify_llm_usables(llm_usables)` | `chatter.py:159` | 注入 / 屏蔽 LLM 工具 |
| `format_message_line(msg, time_format)` | `chatter.py:104` | 静态方法，统一消息行格式 |

注册受 `config.general.enabled` 控制：`False` 时 `get_components()` 不返回 `NeoFatumChatter`，且 `on_config_updated()` 会动态注销已在运行的实例并重启受影响流循环（见 `plugin.py:on_config_updated`）。

### 1.2 Action（LLM 工具）

均为 `BaseAction` 子类，工具名（`action_name`）即 LLM 调用名。

| 类 | action_name | 入口 | 主要参数 |
|---|---|---|---|
| `NFCReplyAction` | `nfc_reply` | `actions/reply.py:25` | `content` `thought` `expected_reaction` `max_wait_seconds` `mood` `reply_to` |
| `DoNothingAction` | `do_nothing` | `actions/do_nothing.py:17` | `thought` `max_wait_seconds` |
| `ScheduleProactiveAction` | `schedule_proactive` | `actions/schedule_proactive.py:32` | `delay_minutes`（30~1440，0 取消）`reason` |
| `QueryActivityPatternAction` | `nfc_query_activity_pattern` | `actions/query_activity_pattern.py:29` | 时间范围查询 |
| `RecordHabitAction` | `nfc_record_habit` | `actions/record_habit.py:16` | 习惯观察写入 session |
| `QueryHabitsAction` | `nfc_query_habits` | `actions/query_habits.py:27` | 按分类查询习惯 |

所有 action `chatter_allow = ["neo_fatum_chatter"]`，仅 NFC 调度时可见。

### 1.3 EventHandler

| 类 | handler_name | 入口 | 订阅事件 |
|---|---|---|---|
| `ProactiveHandler` | `nfc_proactive_handler` | `handlers/proactive_handler.py:27` | `NFC.proactive_trigger` |
| `VoiceCallHistoryHandler` | `nfc_voice_call_history_handler` | `handlers/voice_call_history_handler.py:44` | `voice_call.ended` |

### 1.4 Config

`NFCConfig`（`config.py`）通过 `configs = [NFCConfig]` 注册，框架自动加载 `config/plugins/neo_fatum_chatter/config.toml`。各 section 字段见 §4。

---

## 2. 事件名

NFC 在 `EventBus` 上发布 / 订阅的事件名：

| 事件名 | 发布方 | 订阅方 | payload |
|---|---|---|---|
| `NFC.proactive_trigger` | `NFCPlugin._register_scheduler_tasks` 周期检查触发 | `ProactiveHandler` | `{"stream_id": str, "scheduled_reason": str}` |
| `voice_call.ended` | 外部（语音通话子系统） | `VoiceCallHistoryHandler` | 见框架协议 |

外部插件可通过订阅 `NFC.proactive_trigger` 监听主动发起时机；通过 `publish_event("NFC.proactive_trigger", {...})` 触发主动发起。

---

## 3. 钩子方法（运行时回调）

`NFCPlugin`（`plugin.py:33`）由框架在生命周期节点调用：

| 方法 | 触发时机 | 主要行为 |
|---|---|---|
| `__init__(config)` | 插件加载 | 初始化 `NFCSessionStore` |
| `on_plugin_loaded()` | 加载完成后 | 注册提示词模板；写入 `ScheduleProactiveAction._guidance`；预注册 VLM 跳过；延迟注册调度任务；触发对话中断恢复 |
| `on_config_updated()` | 配置变更后 | 按 `enabled` 动态注册 / 注销 `NeoFatumChatter`，清理 active chatter 并重启受影响流循环 |
| `get_components()` | 框架枚举组件 | 返回需注册的组件列表（`enabled=False` 时排除 Chatter） |

`NeoFatumChatter` 钩子：

| 方法 | 调用方 | 用途 |
|---|---|---|
| `apply_stream_runtime_options` | 框架在流启动 | 写入 `tick_interval_override` / `allow_message_buffer` |
| `modify_llm_usables` | LLM 请求构建 | 注入 NFC actions，过滤 `blocked_tools` |

---

## 4. 配置项（`config.toml`）

配置路径：`config/plugins/neo_fatum_chatter/config.toml`，由 `NFCConfig` 解析为以下 section：

| Section | 入口 | 说明 |
|---|---|---|
| `[general]` | `config.py:GeneralSection` | 启用 / 模型 / 多模态 / 工具调用 / 自定义提示 |
| `[wait]` | `WaitSection` | 等待时长 / 抑制早醒 / 连续超时上限 |
| `[proactive]` | `ProactiveSection` | 主动发起触发条件 / 勿扰时段 / 活跃度服务 |
| `[reply]` | `ReplySection` | 打字节奏 / 分段间隔 / 流式 |
| `[prompt]` | `PromptSection` | 活动流条数 / 压缩 / 摘要 |
| `[buffer]` | `BufferSection` | 消息积累 / LLM 打断 |
| `[flashback]` | `FlashbackSection` | **on_prompt_build 注入点名**（默认 `default_chatter_user_prompt`，对齐 booku_memory；回退 `NFC_user_prompt` 需显式配置） |
| `[debug]` | `DebugSection` | 日志显示开关 |

完整字段表见 `README.md` 的配置章节。

---

## 5. 提示词注入点（on_prompt_build）

`NFCPromptBuilder.build_user_payload`（`prompts/builder.py:81`）在构建 USER payload 时触发 `on_prompt_build` 事件，注入点名由 `config.flashback.injection_point` 决定。

**外部注入器接入流程**：
1. 监听 `on_prompt_build` 事件
2. 比对 `event.payload.prompt_name` 与自身关注的注入点名
3. 返回 `ContextContribution` 列表（`scope = "session" | "turn"`）

注入内容由 `context/planner.py:plan_user_turn` 收集，`scope=session` 的走哈希缓存，`scope=turn` 的每轮独立、通过 `_filter_duplicate_turn_contributions` 去重。

---

## 6. 内部模块入口（开发调试用）

非对外稳定接口，跨版本可能变更：

| 模块 | 关键函数 / 类 |
|---|---|
| `runtime/orchestrator.py` | `execute_orchestrator()` 主循环 |
| `runtime/turn_controller.py` | `prepare_turn_input()` 回合准备 |
| `services/context_sanitizer.py` | `prepare_payload_chain_for_send()` / `close_pending_tool_chain()` |
| `services/timeout_service.py` | `TimeoutService.check_timeout()` / `build_timeout_result()` |
| `services/proactive_service.py` | `ProactiveService.apply_schedule()` |
| `services/summary_service.py` | `SummaryService.maybe_schedule_compression()` |
| `services/multimodal_service.py` | `MultimodalService.append_history_reference()` |
| `services/compressor.py` | `create_compress_request()` / `should_compress()` |
| `protocol/decision_parser.py` | `build_decision()` / `parse_response_decision()` |
| `protocol/response_normalizer.py` | `normalize_response()` |
| `protocol/call_resolver.py` | `normalize_call_name()` / `resolve_registered_call_name()` |
| `protocol/compat_adapter.py` | DeepSeek 等模型的兼容重试 |
| `protocol/perception_retry.py` | `apply_perception_followup()` |
| `context/planner.py` | `ContextPlanner.plan_user_turn()` / `plan_initial_context()` |
| `context/renderer.py` | `ContextRenderer.render_initial_context()` / `render_user_payload()` |
| `persistence/session_store.py` | `NFCSessionStore.get_or_create()` / `save()` / `peek()` |
| `thinker/proactive.py` | `ProactiveThinker.check_all_sessions()` |
| `thinker/timeout_handler.py` | `TimeoutHandler.handle_timeout()` |

---

## 7. 共享数据模型

定义在 `models.py`：

| 符号 | 用途 |
|---|---|
| `NFC_REPLY` / `DO_NOTHING` | 控制流常量（工具名） |
| `ToolCallResult` | LLM 工具调用解析结果（thought / actions / has_reply / has_do_nothing / ...） |
| `extract_visible_reply_text(result)` | 从 ToolCallResult 提取用户可见回复文本 |
| `NFCEventType` | mental_log 事件类型枚举 |
| `WaitingConfig` | 等待状态配置 |

`domain/` 子包定义纯领域模型：

| 类 | 入口 | 说明 |
|---|---|---|
| `NFCSession` | `domain/session_state.py` | 会话状态：mental_log / suppressed_messages / waiting / chain_payloads |
| `Decision` | `domain/decision.py` | 单轮决策结果 |
| `SceneState` | `domain/scene_state.py` | 平台 / 通道 / 时间场景 |
| `TurnTrigger` | `domain/turn_trigger.py` | 回合触发原因枚举 |

---

## 8. 兼容入口

`session.py` re-export `NFCSession` 与 `NFCSessionStore`，旧代码可继续从 `neo_fatum_chatter.session` 导入；新代码请直接从 `domain/` `persistence/` 引用。

`llm_compat.py` 提供 LLM 调用兼容层；`parser.py` 提供旧版纯文本解析路径（保留兼容）。

---

## 许可证

AGPL-3.0
