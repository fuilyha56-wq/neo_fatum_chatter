# NFC 核心机制：内稳态双系统

> v2.6.5 起，NFC 的核心从「心理活动流反射弧」升级为**内稳态双系统循环**。
> 心理活动不再是决策时的一段旁白，而是贯穿时间轴、持续演化、每层都有计算后果的状态场。

```
[潜意识] 内驱状态机 ──── 连续演化、纯本地数学、零 token
    │                    社交欲 / 精力 / 好奇心 / 被忽视感 / 情绪基线
    │                    每 60s tick + 每个事件更新；两次消息之间它也在变
[第一反应] S1 评估 ──── 每批新消息过轻量模型（sub_actor）产出结构化评估：
    │                    情绪 / 紧急度 / 登记簿归属 / 场景剧情事实 / 话题钩子 /
    │                    承诺 / 换挡事件 / 自然延迟建议；失败静默降级
    │                    ★ 与主模型请求并行发射，不阻塞决策
[表达] S2 决策 ─────── 主模型带着内驱快照 + S1 结论做工具调用决策，
    │                    等待时长被内驱调制；thought 从旁白变成有状态的自述
[沉淀] 信念固化 ────── 记忆压缩后用轻量模型蒸馏"对用户/关系的持久判断"，
    │                    带置信度、可强化、可被矛盾证据推翻、随时间衰减
[便签] 备忘录 ──────── LLM 显式写入的中短期事项（带过期时间），
    │                    turn 级渲染进提示词末尾；与信念（自动蒸馏）互补
[欲望] 意图队列 ────── 话题钩子 / 到期承诺 / 内驱溢出以冲动值竞争，
                         最强者过阈值触发主动发起；无意图时回退沉默概率
```

与姊妹插件 kokoro_flow_chatter 的机制级分界：**KFC 是事件驱动的反射弧
（消息来了才活一次，等待是计时器，主动是骰子）；NFC 是时间+事件双驱动的
生命周期系统（无消息时状态仍在演化，主动是欲望溢出，记忆是消化后的理解）。**

---

## 一、内驱状态机（`domain/drives.py`）

五个 0~1 的连续变量，纯数学演化（`advance_to` 按真实流逝时间幂等推进，
磁盘会话加载后无需补算）：

| 维度 | 演化 | 事件扰动 |
|---|---|---|
| social_drive 社交欲 | 空闲缓慢上升（被忽视感高时受抑） | 收到消息↓ 发完话↓ |
| energy 精力 | 随时间恢复（深夜更慢） | 每段回复消耗 |
| curiosity 好奇心 | 缓慢上升 | 超时↑ 消息↓ |
| neglect 被忽视感 | 缓慢衰减 | 迟到回复↑ 及时回应↓ |
| mood 情绪基线 | 向 0.5 回归 | 及回应↑ 超时/迟到↓ S1情绪± |

**消费点**：
- `render_state_text()` 分带量化渲染第一人称状态（"我现在有点累"），
  中性时返回空——同带内字节级一致，保护 prefix cache；
- `modulate_wait()` 调制等待时长（幅度夹 0.5~2.0 倍，`drives.modulation_strength` 可调）；
- `proactive_urge()` 供意图队列计算内驱溢出。

配置：`[drives] enabled / modulation_strength / tick_interval`。

## 二、S1 感知评估（`services/appraisal.py`）

每批新消息（`prepare_turn_input` 的 NEW_MESSAGES 分支）先做一次 sub_actor
结构化评估，输出：`emotion / urgency / register / frame_event / world_facts /
story_update / topic_hooks / commitment / defer_seconds / respond_now / reveal_ids`。

**顾问而非闸门**：超时（默认 8s）、解析失败、模型缺失一律返回 None，
主流程完全退回旧行为。

关键行为：
- **与主模型并行（副决策）**：消息到达即发射评估任务，主决策请求同时
  发出，互不等待。收割完全不阻塞——主响应返回后只收"已完成"的结果
  （未完成的留给下一轮）；评估产物（钩子/承诺/世界事实/换挡）落地后
  影响的是**下一次**决策，绝不干预本轮已发出的回复。
- **自然延迟（下一轮生效）**：`defer_seconds >= 0.5` 且不紧急时设定
  `respond_not_before`；下一批消息的决策请求发起前让出这段等待
  （模拟"看到了，先忙别的"），当前轮的回复不受任何影响。
- **登记簿判定**：隐式分类带粘滞（连续两次一致才换挡），显式 `frame_event`
  立即生效。
- 短于 `appraisal.min_input_chars` 的消息跳过评估（按消息本体字符计量，
  省钱）。

配置：`[appraisal] enabled / model_task / timeout_seconds / defer_enabled /
max_defer_seconds / min_input_chars`。

## 三、双登记簿世界（`domain/world.py`）

**一个角色，两个世界**：

- **现实登记簿**（reality）：沿用 `scene_state`，防幻觉语义（证据地板）；
- **故事登记簿**（story）：被授权虚构的剧情世界状态机（`StoryFacts`：
  故事时间/地点/在场角色/正在进行事件 + 剧情证据）。

换挡四件套（`WorldTracker.apply_frame_event`）：
- `enter_story` 新建/恢复故事世界 → `pause_story` 现实急事（世界保留）
- → `resume_story` 回到剧情 → `exit_story` 有意义的故事**存档**（`story_archive`
  上限 5 档，可 `restore_archive` 续写）。

拟人聊天 / 人设扮演 / 剧情 RP = 同一引擎的三个点：角色深度 × 世界登记。
剧情世界经 turn contribution（transient）渲染进 prompt，不污染持久链。

现实登记簿还包含日程化的 `DailyLifeState`：五段窗口与作息锚点只产生
`routine/inferred` 推断；`planned` 是计划；`self_state_commit` 是角色自己的
当前披露；`observed` 只能由显式观察写入。时间经过、标题相似或模型的
`completed` 字段不会把计划晋升为现实执行事实。`WorldTracker.reality_view()`
提供带 source/epistemic/evidence 标签的结构化现实快照，`nfc_manage_schedule`
统一处理 add/list/confirm/observe/cancel/expire，所有 mutation 走 session lock
并保留稳定 ID、revision 与幂等键。跨日 rollover 清理当日临时条目，跨午夜
活动只在仍覆盖结束窗口时延续。

## 四、信念层（`domain/beliefs.py` + `services/belief_service.py`）

三层记忆：mental_log（事件日志）→ history_summary（叙事压缩）→
**beliefs（消化后的理解）**。每次压缩完成后，sub_actor 从同一批事件文本
蒸馏最多 5 条持久判断（`{statement, subject, confidence, contradicts}`），
合并语义：同信念强化、矛盾削弱、按最后证据时间衰减、上限 30 条。

渲染为"# 你已经知道的"块；跨登记簿只保留 2 条钩子行
（"你们的故事里：……可以在合适的时候自然提起"）——跨世界渗透。

配置：`[beliefs] enabled / model_task / max_extract`。

## 五、意图队列（`domain/intent.py` + `thinker/proactive.py`）

主动发起从掷骰子改为欲望竞争：

- `commitment` 承诺（S1 抽取，`due_hours` 后冲动拉满，**不受勿扰/最小间隔限制**）；
- `topic_hook` 话题钩子（冲动 0.06/h 生长）；
- `drive` 内驱溢出（`proactive_urge() >= 0.6` 时现算）。

`peek_fire`（阈值默认 0.75，打平时承诺优先）→ `mark_triggered` 标记 fired、
释放内驱、把意图内容注入主动发起理由（渲染为"你此前为这次主动联系留过一个念头：
到了你答应过的时间——……"）。无合格意图时回退旧沉默概率兜底。

配置：`[intent] enabled / fire_threshold`。

每次主动判定同时维护 `ProactiveCandidate`：候选拥有稳定 `origin_id` /
`dedupe_key`、route/source、preferred/best/expire 窗口和
`queued/deferred/blocked/dispatching/sent/expired/cancelled` 生命周期。候选只是审计与
去重层，不是发送保证；勿扰、冷却、暂停等 gate 会记录阻塞原因，用户新消息
会取消冲突的 continuation，实际触发后才标记 `sent`，并随 session JSON 恢复。

### 主动回合的工具能力

主动回合与普通回合同用一个 `usable_map`（execute 生命周期内 `inject_usables`
一次性注册，含全部第三方工具如 nai 发图），触发占位消息会作为 `trigger_msg`
传给工具执行器——**执行链路上主动发图从来不是问题，限制只曾在提示词层**。
当前版本起提示词不再把工具集收窄为 nfc_reply/do_nothing：

- 每轮 user_text 末尾"重申"、主动决策指令（`NFC_PROACTIVE_DECISION_TOOL_CALLING`）、
  感知→决策跟进提示（`NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING`）均改为
  "除基础动作外可组合调用其他已注册工具，按调用顺序依次执行"；
- 模型一次响应发多个工具调用时，解析器按序执行（第三方批量 flush、reply 前
  先 flush 已积累的第三方调用、查询类工具回传结果后续轮再决策）。

主动思考提示词模板可通过 `[prompt] proactive_prompt_override` 自定义
（占位：`{current_time}` `{silence_duration}` `{recent_activity}`
`{proactive_decision_instruction}`；校验 XML 配对 + 占位白名单，违规回退默认）。
`build_proactive_context` 每次触发时重新解析模板，配置热更新后下一次主动发起
立即生效；`register_nfc_prompts` 同步改为覆盖式注册（`register_template`），
修复了 system_prompt_override 热重载不生效的存量缺陷。

## 六、备忘录（`domain/memo.py` + `actions/memo.py`）

第四层记忆：**LLM 显式标记的中短期便签**。与 mental_log（自动事件流）、
history_summary（叙事压缩）、beliefs（消化后的持久判断）互补——覆盖
"接下来一段时间需要明确意识到的事"，语义上更接近贴在脑门上的便签。

- `nfc_memo(content, intent, expire_hours)` 写入或刷新（content 相同视为
  同一条：刷新过期时间与动机、保留原创建时间）；`nfc_memo_delete(memo_ids)`
  按 id 清理已兑现/不再相关的条目。写/删事件入 mental_log 作审计。
- 容量与寿命：单流默认上限 10 条（超出按创建时间淘汰最早），过期时长
  1h~14d（默认 24h）兜底——但依赖模型主动删除，过期只是兜底。
- 渲染：`MemoBook.render()` 经 state_source 作为 turn 级 contribution
  （owner=notice, priority=80）注入提示词末尾，不进持久链、不影响前缀
  缓存；剩余时间按小时取整避免渲染抖动；无有效条目时不渲染。
- `reset_context`（清空上下文）不删除备忘——它是模型的自主笔记而非对话
  内容，与习惯/信念同为长期资产，靠过期时间自然消亡。

配置：`[memo] enabled / max_entries / default_expire_hours /
min_expire_hours / max_expire_hours`。

## 七、角色卡三层（`domain/character_card.py` + `[character]` 配置）

- **红线**（`redlines`）：无论什么情况都不会做的事——OOC 最后闸门；
  非空时只读追加到 core.toml 的 `safety_guidelines` /
  `negative_behaviors` 末尾随系统提示词生效（热重载同步），
  不再单独渲染；
- **隐藏事实**（`hidden_facts`：`{fact, condition}`）：**平时不进 prompt**，
  S1 判定 `reveal_ids` 满足揭示条件后才注入——角色因此可以欲言又止、有秘密可揭；
- **剧情覆层**（overlay）：入戏时套上的临时戏服，出戏摘除——
  "演角色时她听起来还是她"（覆层渲染自带'底层人格会漏出来'提示）。

揭示状态与覆层随 session 持久化（`character_state`）。

## 八、节奏引擎

- **语义化打字延迟**（`execution/reply_executor.compute_segment_delay`）：
  段间延迟 = 下一段字数/`typing_chars_per_sec` + 上一段结尾标点停顿
  （省略号 0.8s > 问句 0.6s > 陈述 0.25s）+ ±20% 抖动，夹在
  `segment_delay_min/max`。`reply.semantic_delays` 开关。
- **S1 自然延迟**：见上文。

## 九、从 KFC 吸收的设计（v2.6.5）

1. **超时提示 request-only 化**：超时 payload 改经 RequestView transient 注入，
   发送后剥离，不再永久残留 response 链（旧实现会被 request_snapshot 带着重启）；
2. **打断冷却递增 + 连续上限**：`buffer.interrupt_cooldown ×(1+0.5×(n-1))`，
   连续 `max_consecutive_interrupts`(3) 次后不再打断，防高频消息拖死 LLM 调用；
3. **工具列表稳定排序**：`modify_llm_usables` 按组件签名排序，保护 prefix cache。

v2.6.5 同版补齐第四项：**显式备忘录**（memo 便签，见第六节）——KFC 的
`kfc_memo` / `kfc_memo_delete` 语义等价移植，渲染层复用 NFC 的 turn 级
contribution 管线。

## 十、迁移与兼容

- 旧 session JSON 加载自动获得默认值（`from_dict` 全字段防御）；
- 新配置全部有默认值且默认开启核心项；每一层独立可关
  （drives/appraisal/beliefs/intent/memo 关闭后完全回到旧版反射弧行为）；
- `context_clear`（清空上下文）语义：活跃故事世界清除，但信念/意图/习惯/
  内驱/备忘录/故事存档作为长期资产保留。

## 十一、验证

- `tests/test_drives.py / test_world_state.py / test_beliefs.py /
  test_intent_queue.py / test_character_card.py / test_appraisal.py /
  test_state_and_delays.py / test_timeout_transient.py / test_memo.py`
- 端到端模拟：`python tests/simulate_core_mechanism.py`
  （两天生命周期的状态快照：内驱演化 → 承诺到期主动 → 入戏/暂停/出戏存档 → 渲染）
