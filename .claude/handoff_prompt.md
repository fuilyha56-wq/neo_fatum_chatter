# NFC 群聊模块实施交接提示词

## 直接粘贴到新对话中使用

---

## 背景

我在 `E:\Neo-mofox-instance\bot-3693525299\neo-mofox\plugins\neo_fatum_chatter` 下为 NeoFatumChatter (NFC) 插件完成了群聊模式的实现。群聊状态机完全照搬 DefaultChatter (DFC，位于 `plugins/default_chatter/`) 的逻辑。

### 已完成的工作

**新增文件：**
- `actions/send_text.py` — `nfc_send_text` 动作（群聊发送文本，照搬 DFC `SendTextAction`）
- `actions/pass_and_wait.py` — `nfc_pass_and_wait` 动作（登记等待）
- `actions/stop_conversation.py` — `nfc_stop_conversation` 动作（结束对话+冷却）
- `prompts/group_templates.py` — 群聊系统/用户/sub-agent 提示词模板（照搬 DFC 模板结构）
- `prompts/group_builder.py` — 群聊提示词构建器与注册函数
- `protocol/group_decision.py` — sub-agent 决策模块（照搬 DFC `decision_agent.py`）
- `runtime/group_gate.py` — 群聊门控（概率+sub-agent 判定，照搬 DFC `_compute_sub_agent_bypass_probability`）
- `runtime/group_orchestrator.py` — 群聊 DFC 风格四相状态机
- `runtime/_group_tool_flow.py` — 群聊工具调用控制流（照搬 DFC `tool_flow.py`）

**修改文件：**
- `config.py` — 新增 `GroupSection`（enable_cooldown/enable_action_suspend/enable_programmatic_controller/native_multimodal/reinforce_negative_behaviors/enable_llm_stream 等）+ `PromptsSection`
- `chatter.py` — `chat_type` 改为 `ChatType.ALL`，`execute()` 按 chat_type 分流
- `plugin.py` — 导入新 action，注册群聊提示词模板，`get_components()` 注册三个新 action
- `runtime/__init__.py` — 导出 `execute_group_orchestrator`
- `actions/__init__.py` — 导出三个新 action
- `manifest.json` — 声明三个新 action 组件

**配置（已自动生成）：**
- `config/plugins/neo_fatum_chatter/config.toml` — `[group]` 和 `[prompts]` 段

### 设计原则（必须遵守）

1. **群聊状态机 = DFC 原样** — 四相 FSM (WAIT_USER → MODEL_TURN → TOOL_EXEC → FOLLOW_UP) 完全照搬 DFC `session.py`
2. **上下文构建 = DFC 原样** — response 链复用，保证 KV 缓存命中率
3. **私聊和群聊完全隔离** — 群聊不经过 mental_log / chain_payloads / scene_state / interrupt_controller / NFCSession 等私聊特有功能
4. **Action 名称用 `nfc_*` 前缀** — 避免与 DFC 同时加载时冲突

### 51 个现有测试全部通过

---

## 需要你完成的任务

### 任务1：审查 NFC 全量代码 bug 扫描与修复

扫描以下所有 NFC 源文件，找出潜在 bug（类型错误、import 遗漏、逻辑缺陷、与框架 API 不兼容等），找到后直接修复：

扫描范围：
- `plugins/neo_fatum_chatter/` 下的所有 `.py` 文件
- 重点关注：chatter.py、plugin.py、config.py、所有 actions/、prompts/、protocol/、runtime/ 下的文件

参考标准：DFC 源文件（`plugins/default_chatter/`）

### 任务2：审查群聊状态机代码 bug 扫描与修复

深度审查以下群聊专属文件，对照 DFC 源文件逐行比对逻辑正确性：

审查文件：
- `runtime/group_orchestrator.py` ← 对照 `plugins/default_chatter/session.py`
- `runtime/_group_tool_flow.py` ← 对照 `plugins/default_chatter/tool_flow.py`
- `runtime/group_gate.py` ← 对照 DFC `plugin.py` 中的 `sub_agent()` + `_compute_sub_agent_bypass_probability()`
- `protocol/group_decision.py` ← 对照 `plugins/default_chatter/decision_agent.py`
- `prompts/group_builder.py` ← 对照 `plugins/default_chatter/prompt_builder.py`
- `prompts/group_templates.py` ← 对照 DFC `plugin.py` 中的模板文本
- `actions/send_text.py` ← 对照 DFC `plugin.py` 中的 `SendTextAction`
- `actions/pass_and_wait.py` ← 对照 DFC `plugin.py` 中的 `PassAndWaitAction`
- `actions/stop_conversation.py` ← 对照 DFC `plugin.py` 中的 `StopConversationAction`

重点检查：
- tool_flow 的 control call name 是否匹配 action_name（`action-nfc_pass_and_wait` / `action-nfc_stop_conversation`）
- group_orchestrator 中 flush_unreads 的调用时机是否正确
- gate 中概率计算逻辑是否与 DFC 完全一致
- decision 中 token 裁剪逻辑是否完整
- 多模态 import 路径是否正确（`from plugins.default_chatter.multimodal import ...` 是否可行，还是应该用相对导入）

### 任务3：审查代理1和代理2的修复结果

在任务1和任务2的修复完成后：
- 验证修复是否引入新问题
- 运行 `pytest plugins/neo_fatum_chatter/tests/ -v` 确认测试通过
- 运行导入验证确认无 ImportError

### 任务4：遗漏检查

检查是否有以下遗漏：
- DFC 的某个功能分支未被移植
- 某个配置项声明了但未在代码中使用
- 某个 action 注册了但 orchestrator 中未对应处理
- prompt 模板中的占位符与 builder 传入的变量不匹配
- manifest.json 与实际组件不一致

### 任务5：总结审查

汇总所有发现和修复，给出最终状态报告。

---

## 关键路径参考

| NFC 群聊文件 | 对应 DFC 源文件 |
|---|---|
| `runtime/group_orchestrator.py` | `plugins/default_chatter/session.py` |
| `runtime/_group_tool_flow.py` | `plugins/default_chatter/tool_flow.py` |
| `runtime/group_gate.py` | `plugins/default_chatter/plugin.py:684-718` + `plugin.py:867-907` |
| `protocol/group_decision.py` | `plugins/default_chatter/decision_agent.py` |
| `prompts/group_builder.py` | `plugins/default_chatter/prompt_builder.py` |
| `prompts/group_templates.py` | `plugins/default_chatter/plugin.py:86-262` |
| `actions/send_text.py` | `plugins/default_chatter/plugin.py:266-483` |
| `actions/pass_and_wait.py` | `plugins/default_chatter/plugin.py:485-502` |
| `actions/stop_conversation.py` | `plugins/default_chatter/plugin.py:505-519` |
| `config.py` GroupSection | `plugins/default_chatter/config.py` PluginSection |

## 注意事项

- DFC 位于 `E:\Neo-mofox-instance\bot-3693525299\neo-mofox\plugins\default_chatter\`
- NFC 位于 `E:\Neo-mofox-instance\bot-3693525299\neo-mofox\plugins\neo_fatum_chatter\`
- 群聊启用条件：`config.toml` 中 `[group] enabled = true`
- 所有群聊 action 的 `chatter_allow = ["neo_fatum_chatter"]`
- 群聊 tool_flow 中的 control call name 格式为 `action-{action_name}`，即 `action-nfc_pass_and_wait` / `action-nfc_stop_conversation`
- 群聊 orchestrator 中 multimodal import 使用 `from plugins.default_chatter.multimodal import build_multimodal_content, extract_images_from_messages`（复用 DFC 的 multimodal helper，**需要审查此路径是否可行**）
- NFC 私聊特有功能（mental_log / chain_payloads / scene_state / interrupt_controller / perceive_loop）在群聊路径中**绝对不能**出现
