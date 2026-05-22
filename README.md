# Neo Fatum Chatter (NFC)

> **版本选择说明（重要，置顶）**
>
> 当前 GitHub Release 保留两个可下载版本；插件市场默认安装最新版本。
>
> | 版本 | 定位 | 优点 | 缺点 / 注意 |
> |------|------|------|-------------|
> | `v2.0.2` | 普通版 / baseline 修复版 | 保留原上下文行为，同时包含 manifest 身份修复、系统提示词空值修复、作者信息更新；适合担心缓存优化影响行为的人 | 没有进一步强化 LLM prompt prefix cache 命中，厂商缓存折扣收益较低 |
> | `v2.0.4` | 超高缓存命中版（默认推荐） | 插件正式更名为 Neo Fatum Chatter，并继承 v2.0.3 的 prefix cache 优化：去掉相对分钟数、降低无历史时间精度、冻结融合叙事；更容易吃到 DeepSeek / Claude / Qwen / OpenAI 等厂商的 prompt cache 折扣 | 首次生成冻结叙事时会多保存一次 session；组件名称已变更为 `neo_fatum_chatter` |
>
> 兼容性说明：当前市场仅保留 `v2.0.2` 与 `v2.0.4`。`v2.0.4` 是推荐版本；如需更保守的原上下文行为，可手动选择 `v2.0.2`。

*Fatum — 拉丁语中“命运”的意思。*

**Neo Fatum Chatter 对话引擎 — 基于心理活动流的私聊特化聊天器** — Neo-MoFox 插件

---

## 概述

NFC 是一个面向私聊场景的 Chatter 插件，核心设计是将 LLM 的每次决策与内心独白（MentalLog）绑定，形成连续的心理活动流。对话历史与内心活动按时间线交织，让模型在回复时不仅能看到说了什么，还能“回想起”当时在想什么。

**主要能力**

- 每次回复附带内心独白，记录当前情绪与期待
- 显式维护 SceneState，避免把平台/私聊通道自动脑补成生活场景
- 等待超时后分析消息类型，决定追问、继续等或结束
- 沉默超过阈值后有概率主动发起对话，深夜自动静默
- 多条连发消息在积累窗口内合并后统一处理
- LLM 生成期间若检测到新消息，取消当前请求并重新处理
- 原生多模态支持，图片直接进 LLM 上下文
- 回复拆分为短句模拟打字节奏逐条发送

---

## 架构

### 单一决策协议

NFC 当前正式只保留一条内部协议：

tool calling -> response_normalizer -> Decision

运行时主流程已从 `chatter.py` 中抽离到 `runtime/orchestrator.py`，`NeoFatumChatter` 主要保留门面与 helper。回合准备与提交下沉到 `runtime/turn_controller.py`，消息积累窗口与 LLM 打断控制分别收敛在 `runtime/message_buffer.py`、`runtime/interrupt_controller.py`。超时、主动预约、近期摘要、多模态历史图片注入等外围副作用通过 `services/` 目录进入主流程。

核心动作如下：

| 动作 | 用途 |
|------|------|
| `nfc_reply` | 发送消息，携带 `content`、`thought`、`expected_reaction`、`max_wait_seconds`、`mood` |
| `do_nothing` | 选择不回复，携带 `thought`、`max_wait_seconds` |
| `schedule_proactive` | 预约下一次主动思考时间，携带 `delay_minutes`、`reason` |

### 消息积累窗口

检测到第一条新消息后，NFC 等待一个固定窗口（默认 1.5 秒）以收集连发的多条消息，再统一提交给 LLM。最大积累时长默认为 5 秒，防止无限延迟。

### LLM 生成打断

LLM 生成期间，每 0.5 秒检测一次是否有新消息到达。检测到打断时会取消当前请求，重新积累新消息并重新提交。

### 超时与主动发起

回复后可以进入等待状态；等待超时后会重新注入上下文，让 LLM 决策追问、放弃或继续等待。长时间沉默时，主动发起检查会按配置决定是否触发新对话。

---

## 配置

配置文件路径：`config/plugins/neo_fatum_chatter/config.toml`

主要配置分组：

- `[general]`：模型任务、模型列表、原生多模态、图片配额、工具屏蔽、自定义决策提示词
- `[wait]`：等待回复开关、最小/最大等待秒数、连续超时上限
- `[proactive]`：主动发起开关、沉默阈值、触发概率、勿扰时段、检查间隔、预约指导语
- `[reply]`：模拟打字速度与延迟范围
- `[prompt]`：活动流最大条目、上下文 payload 上限、近期摘要压缩策略
- `[buffer]`：消息积累窗口、最大积累时长、LLM 生成打断轮询间隔
- `[debug]`：提示词与响应摘要日志开关

---

## 安装

通过 Neo-MoFox 插件市场安装：

```bash
mpdt market install neo_fatum_chatter
```

或从 GitHub Release 手动下载指定版本 `.mfp` 文件放入 `plugins/` 目录。

---

## 许可证

与 Neo-MoFox 主项目保持一致。
