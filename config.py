"""NeoFatumChatter 配置定义。

定义插件所有可配置参数，基于 Pydantic + TOML 热重载。
通过 @config_section 划分为语义清晰的 Section。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import field_validator, model_validator

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

from .prompts.templates import NFC_SYSTEM_PROMPT


class NFCConfig(BaseConfig):
    """NeoFatumChatter 配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "NeoFatumChatter 配置"

    @config_section("general")
    class GeneralSection(SectionBase):
        """基础配置。"""

        enabled: bool = Field(default=True, description="是否启用")
        model_task: str = Field(
            default="actor",
            description="LLM 模型名称（对应 model.toml 中的 task），models 为空时使用",
        )
        models: list[str] = Field(
            default_factory=list,
            description="指定 LLM 模型列表（对应 model.toml 中的 name）。非空时覆盖 model_task，多个模型按顺序 fallback",
        )
        temperature: float = Field(
            default=0.7,
            description="模型温度，仅在 models 非空时生效",
        )
        max_tokens: int = Field(
            default=8000,
            description="最大输出 token 数，仅在 models 非空时生效",
        )
        native_multimodal: bool = Field(
            default=False,
            description=(
                "原生多模态模式。启用后，图片直接打包进 LLM payload，"
                "由主模型在对话上下文中理解图片内容并做出响应。"
                "需确保 model_task 配置的模型支持多模态输入。"
            ),
        )
        max_images_per_payload: int = Field(
            default=4,
            description=(
                "原生多模态模式下的总图片配额（整个 payload 中所有来源的图片上限）。"
                "配额由 bot 已发图片、用户新消息图片、历史图片三者共同占用，"
                "优先级依次为：bot 已发 > 用户新消息 > 历史补充。"
                "例如设为 4 时，若 bot 最近发了 1 张、用户本轮发了 2 张，则历史图片最多补 1 张。"
            ),
        )
        use_tool_calling: bool = Field(
            default=True,
            description=(
                "（已废弃）历史上用于切换主动发起/超时上下文是否使用工具调用决策提示。"
                "NFC 当前统一走工具调用协议，此字段不再生效，保留仅为向后兼容旧配置。"
            ),
        )
        max_compat_retries: int = Field(
            default=1,
            description=(
                "纯文本感知草稿未形成工具调用时的最大重试次数。"
                "NFC 会把该轮输出视为未发送草稿，并注入 tool-call 约束后重试。"
                "0 表示不重试。"
            ),
        )
        perception_extract_task: str = Field(
            default="sub_actor",
            description=(
                "感知阶段兜底回填时，用于提取回复内容的模型任务名称。"
                "设为 'sub_actor' 使用轻量模型（默认，省开销），"
                "设为 'actor' 使用主对话模型（更懂上下文风格）。"
                "对应 model.toml 中的 task 名称。"
            ),
        )
        max_empty_reply_retries: int = Field(
            default=2,
            description=(
                "模型调用 nfc_reply 但 content 为空（空包弹）时的最大重试次数。"
                "NFC 会注入提示要求模型重新生成有效回复内容后再次发送。"
                "0 表示不重试，直接放行空回复（本轮不发任何消息）。"
            ),
        )
        max_consecutive_llm_failures: int = Field(
            default=15,
            description=(
                "连续 LLM 请求失败的最大容忍次数。"
                "超过此值后终止当前会话循环并报告失败。"
                "设为 0 则不限制（保持无限重试）。"
            ),
        )
        custom_decision_prompt: str = Field(
            default="",
            description=(
                "自定义决策提示词。用于指导 NFC 的决策行为，"
                "会被注入到系统提示词的安全准则之后。留空则不生效。"
            ),
        )
        blocked_tools: list[str] = Field(
            default_factory=lambda: ["send_text", "pass_and_wait", "stop_conversation"],
            description=(
                "需要从工具列表中屏蔽的工具末段名称（不含组件类型前缀）。"
                "列表中的工具不会暴露给 LLM。"
            ),
        )
        segment_instruction: str = Field(
            default=(
                "## 消息分段发送\n"
                "你可以把回复拆成多条消息分开发送，模仿真人边想边打字的节奏，想到什么就发什么。\n"
                "将每条独立消息作为数组中的一个元素传入 content，系统会自动依次发出。\n\n"
                "**分段建议**：\n"
                "- 随意分段，不必凑完整句子，话说到一半想到新的可以直接断开；\n"
                "- 语气词、口语转折词、感叹词出现时是天然的分段点；\n"
                "- 每段尽量短，几个字到十几字最自然；\n"
                "- 同一个意思可以拆开几条说，前一条留悬念，后一条接上；\n"
                "- 只有一两个字时可以不分段。"
            ),
            description=(
                "注入到提示词中的自定义分段指令。"
                "留空则不注入任何分段指导。"
            ),
        )
        wait_instruction: str = Field(
            default=(
                "### max_wait_seconds（等待时长）\n\n"
                "这个参数描述的是你发完消息后是否在等回复。\n\n"
                "期待对方很快回应——填一个短时间（比如你问了个问题、聊得正起劲想继续）。\n"
                "话题告一段落、说了告别、对方不需要特别回什么——填 0。\n\n"
                "用短等待来维持当前聊天的节奏；如果是想过一段时间再主动找对方，"
                "那是主动思考工具的用途，不是这里。"
            ),
            description=(
                "注入到提示词中的 max_wait_seconds 等待时长指导说明。"
                "留空则不注入。"
            ),
        )
        enable_custom_tick_interval: bool = Field(
            default=False,
            description=(
                "是否启用 NFC 独立的主循环 tick 间隔。"
                "关闭时跟随主程序 bot.tick_interval 全局配置；"
                "开启时使用下方 custom_tick_interval 覆盖该 stream 的 tick 间隔。"
            ),
        )
        custom_tick_interval: float = Field(
            default=5.0,
            description=(
                "NFC 独立主循环 tick 间隔（秒），仅在 enable_custom_tick_interval 为 true 时生效。"
                "过短会增加消耗，过长会降低响应速度。必须大于 0。"
            ),
        )

        @field_validator("custom_tick_interval", mode="after")
        @classmethod
        def _clamp_custom_tick_interval(cls, value: float) -> float:
            """custom_tick_interval 必须为正数。"""
            v = float(value)
            return v if v > 0 else 5.0

    @config_section("wait")
    class WaitSection(SectionBase):
        """等待机制配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用回复等待。设为 false 后模型不再等待用户回复",
        )
        min_seconds: float = Field(default=10.0, description="最小等待秒数")
        max_seconds: float = Field(default=600.0, description="最大等待秒数")
        max_consecutive_timeouts: int = Field(
            default=3, description="连续超时上限，达到后不再等待"
        )
        suppress_early_wake: bool = Field(
            default=True,
            description=(
                "等待期间收到新消息时是否抑制提前唤醒。"
                "开启后，Bot 在等待超时到达前不会因为新消息提前触发 LLM，"
                "所有消息在等待结束后统一处理。"
            ),
        )

        def apply_rules(self, raw_seconds: float, consecutive_timeouts: int) -> float:
            """应用等待时长规则。raw_seconds <= 0 或 enabled=false 时返回 0。"""
            if not self.enabled or raw_seconds <= 0:
                return 0.0
            if consecutive_timeouts >= self.max_consecutive_timeouts:
                return 0.0
            return max(self.min_seconds, min(raw_seconds, self.max_seconds))

        @model_validator(mode="after")
        def _order_wait_bounds(self) -> "NFCConfig.WaitSection":
            """确保等待上下限顺序正确。"""
            self.min_seconds = max(0.0, float(self.min_seconds))
            self.max_seconds = max(0.0, float(self.max_seconds))
            if self.min_seconds > self.max_seconds:
                self.min_seconds, self.max_seconds = self.max_seconds, self.min_seconds
            self.max_consecutive_timeouts = max(0, int(self.max_consecutive_timeouts))
            return self

    @config_section("proactive")
    class ProactiveSection(SectionBase):
        """主动发起配置。"""

        enabled: bool = Field(default=True, description="是否启用主动发起")
        silence_threshold: int = Field(
            default=7200, description="沉默阈值(秒)，超过后可能主动发起"
        )
        trigger_probability: float = Field(
            default=0.3, description="主动发起触发概率"
        )
        min_interval: int = Field(
            default=1800, description="两次主动发起最小间隔(秒)"
        )
        quiet_hours_start: str = Field(default="23:00", description="勿扰开始时间")
        quiet_hours_end: str = Field(default="07:00", description="勿扰结束时间")
        check_interval: int = Field(
            default=60, description="主动发起检查间隔(秒)"
        )
        schedule_guidance: str = Field(
            default=(
                "预约是你和 Ta 保持长线联系的主要方式，不是偶尔才想到的工具。\n\n"
                "每次对话之后，自然地想一想「下次什么时候联系 Ta？」，有想法就设一下，"
                "让联系成为持续的节律，而不是单次事件。大部分时候保持有一个预约在等待中就好，不必强迫每次都设。\n\n"
                "**发出消息之后：**\n"
                "根据这次对话，考虑一下下次的时间，有感觉就设上。\n\n"
                "**触发时刚好在聊天中：**\n"
                "通常不用额外发，继续聊就好。聊完之后可以再想想下一次。\n\n"
                "**触发时觉得时机不对：**\n"
                "不发也行，可以重新设一个更合适的时间。\n\n"
                "**频率没有固定规律：**\n"
                "跟着你们当下的状态走，热络时短一些，需要空间时长一些。\n\n"
                "系统的沉默触发只是兜底，预约才是主力。"
            ),
            description="schedule_proactive 工具的使用场景指导（会展示在工具描述中，可按需自定义）",
        )
        activity_service_signature: str = Field(
            default="",
            description=(
                "活跃度判断服务的签名（如 better_chat_time:service:better_chat_time）。"
                "为空时使用内置的 is_user_typically_active_now()。"
                "配置后 ProactiveThinker 优先调此服务的方法来判断活跃度。"
            ),
        )
        activity_service_method: str = Field(
            default="is_good_time",
            description="活跃度服务上调用的方法名，该方法需接受 (stream_id: str) 返回 float 0~1",
        )

        @field_validator("trigger_probability", mode="after")
        @classmethod
        def _clamp_trigger_probability(cls, value: float) -> float:
            """将主动触发概率限制在 [0, 1]。"""
            return max(0.0, min(float(value), 1.0))

        @field_validator("min_interval", "check_interval", mode="after")
        @classmethod
        def _positive_intervals(cls, value: int, info) -> int:
            """主动触发间隔必须为正数。"""
            v = int(value)
            if v > 0:
                return v
            return 1800 if info.field_name == "min_interval" else 60

    @config_section("reply")
    class ReplySection(SectionBase):
        """回复配置。"""

        typing_chars_per_sec: float = Field(
            default=15.0, description="模拟打字速度(字/秒)"
        )
        typing_delay_min: float = Field(
            default=0.8, description="最小打字延迟(秒)"
        )
        typing_delay_max: float = Field(
            default=4.0, description="最大打字延迟(秒)"
        )
        segment_delay_min: float = Field(
            default=0.5,
            description="多段消息之间的最小间隔(秒)，模拟真人打完一条再打下一条的节奏",
        )
        segment_delay_max: float = Field(
            default=2.0,
            description="多段消息之间的最大间隔(秒)",
        )
        semantic_delays: bool = Field(
            default=True,
            description=(
                "启用语义化打字延迟：段间延迟按下一段的字数（typing_chars_per_sec）、"
                "标点语气（问句/感叹/省略号）动态计算，而非纯随机。"
                "关闭则退回 segment_delay_min/max 均匀随机。"
            ),
        )
        streaming_enabled: bool = Field(
            default=False,
            description=(
                "是否启用流式回复（打字机效果）。启用后，长消息会分块逐步发送，"
                "模拟真人边打字边发送的体验。需要平台适配器支持编辑消息。"
            ),
        )
        streaming_service_signature: str = Field(
            default="",
            description=(
                "流式回复 Service 签名。"
                "留空时自动发现支持 start_streaming 的 Service。"
            ),
        )
        streaming_chunk_size: int = Field(
            default=10,
            description="流式回复每次追加的字符数",
        )
        streaming_interval: float = Field(
            default=0.1,
            description="流式回复每次追加之间的间隔(秒)",
        )

        @model_validator(mode="after")
        def _order_segment_delay_bounds(self) -> "NFCConfig.ReplySection":
            """规整多段回复延迟范围。"""
            if float(self.segment_delay_min) < 0:
                self.segment_delay_min = 0.5
            if float(self.segment_delay_max) < 0:
                self.segment_delay_max = 2.0
            if self.segment_delay_min > self.segment_delay_max:
                self.segment_delay_min, self.segment_delay_max = (
                    self.segment_delay_max,
                    self.segment_delay_min,
                )
            return self

    @config_section("prompt")
    class PromptSection(SectionBase):
        """提示词配置。"""

        request_snapshot_enabled: bool = Field(
            default=True,
            description=(
                "保存每次实际发送给模型的完整请求体，并在进程重启后的首次 NFC 请求中恢复。"
            ),
        )

        summary_enabled: bool = Field(
            default=True,
            description=(
                "是否启用近期记忆摘要。"
                "关闭后不再触发摘要压缩任务，也不再向提示词注入 history_summary；"
                "session 中已存在的旧摘要不会被清除，但不会再出现在上下文中。"
            ),
        )
        max_log_entries: int = Field(
            default=50, description="最大活动流条目数"
        )
        max_context_payloads: int = Field(
            default=20, description="LLM 上下文持久化链最大条目数（超出时裁剪最旧的 USER/ASSISTANT 对）"
        )
        max_initial_chain_payloads: int = Field(
            default=12,
            description="execute 启动时最多恢复进 LLM 的持久化 chain payload 条数，不影响持久化保留数量",
        )
        max_fused_narrative_chars: int = Field(
            default=12000,
            description="融合叙事最大字符数，超出时仅保留最近部分，降低框架 token 裁剪触发概率",
        )
        compress_every_n_rounds: int = Field(
            default=50,
            description="每完成 N 轮对话触发一次近期记忆压缩（1 轮 = 1 次 USER→ASSISTANT 交换）",
        )
        compress_days_window: float = Field(
            default=3.0,
            description="压缩时覆盖的历史时间窗口（天），只对该窗口内的消息做摘要",
        )
        min_compress_interval_minutes: float = Field(
            default=120.0,
            description="两次压缩之间的最短间隔（分钟），防止频繁触发",
        )
        system_prompt_override: str = Field(
            default=NFC_SYSTEM_PROMPT,
            description=(
                "系统提示词模板。\n"
                "\n"
                "默认已填入 NFC 标准模板，可直接在此修改并保存。\n"
                "\n"
                "标准模板源码位置：prompts/templates.py 中的 NFC_SYSTEM_PROMPT。\n"
                "\n"
                "若修改后想恢复原状，请删掉本行（整行 system_prompt_override 配置），\n"
                "下次启动框架会自动用标准模板重新填回。\n"
                "\n"
                "保存时会校验：\n"
                "- 所有 XML 标签开闭配对；\n"
                "- 所有 {占位} 必须是 NFC 可渲染的占位名；\n"
                "- 必须包含 6 大核心标签：<existence_logic>、<personality>、"
                "<behavioral_guidance>、<the_inner_voice>、<tool_usage>、<extra_context>。\n"
                "任一不满足则打回标准模板并在日志记录。"
            ),
            label="系统提示词自定义",
            input_type="textarea",
            rows=24,
            placeholder="默认已填入标准模板，可直接修改",
            tag="prompt",
        )


    @config_section("buffer")
    class BufferSection(SectionBase):
        """消息积累与打断配置。"""

        accumulate_window: float = Field(
            default=1.5,
            description=(
                "消息积累窗口（秒）。检测到第一条消息后等待此时长，"
                "以收集同一时段连发的多条消息，避免对每条消息单独触发 LLM。"
                "设为 0 则禁用积累窗口。"
            ),
        )
        accumulate_max_window: float = Field(
            default=5.0,
            description=(
                "积累窗口最大总时长（秒）。即使消息持续到达，"
                "超过此时长后强制提交，防止积累无限延迟。"
            ),
        )
        interrupt_enabled: bool = Field(
            default=True,
            description=(
                "是否启用 LLM 生成打断。启用后，LLM 生成期间若检测到"
                "新消息到达，将取消当前 LLM 请求并以全量消息重新发起。"
            ),
        )
        interrupt_poll_seconds: float = Field(
            default=0.5,
            description=(
                "打断检测轮询间隔（秒）。LLM 生成期间每隔此时间检查"
                "一次是否有新消息到达。值越小响应越快，CPU 占用略高。"
            ),
        )
        interrupt_cooldown: float = Field(
            default=3.0,
            description=(
                "打断后冷却基准时长（秒）。打断后等待此时长再重新发起请求，"
                "以收集可能连发的后续消息；连续打断时冷却时间递增。"
            ),
        )
        max_consecutive_interrupts: int = Field(
            default=3,
            description=(
                "连续打断上限。达到后不再打断 LLM 生成，等本次请求完成后"
                "统一处理新消息，防止高频消息把 LLM 调用拖入无限重启。"
            ),
        )

        @field_validator("accumulate_window", "accumulate_max_window", "interrupt_poll_seconds", mode="after")
        @classmethod
        def _clamp_non_negative(cls, value: float) -> float:
            """将消息缓冲与轮询时间规整为非负数。"""
            return max(0.0, float(value))

    @config_section("flashback")
    class FlashbackSection(SectionBase):
        """注入点兼容配置。"""

        injection_point: str = Field(
            default="default_chatter_user_prompt",
            description=(
                "NFC user prompt 构建时触发的 on_prompt_build 事件注入点名称。"
                "默认对齐 booku_memory 等主流注入器订阅的 default_chatter_user_prompt；"
                "需要回退到 NFC 私有注入点名时改为 NFC_user_prompt。"
            ),
        )

        @field_validator("injection_point", mode="after")
        @classmethod
        def _validate_injection_point(cls, value: str) -> str:
            """空字符串回退到默认注入点名。"""
            v = (value or "").strip()
            if not v:
                return "default_chatter_user_prompt"
            return v

    @config_section("drives")
    class DrivesSection(SectionBase):
        """内驱状态机配置（角色的潜意识层）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用内驱状态机。启用后角色拥有随时间演化的内部状态"
                "（社交欲/精力/好奇心/被忽视感/情绪基线），并调制等待时长、"
                "渲染进提示词。关闭则完全回到旧行为。"
            ),
        )
        modulation_strength: float = Field(
            default=0.3,
            description=(
                "内驱对等待时长的调制强度（0~1）。0 = 只展示状态不影响数值，"
                "越大影响越明显（调制倍率被夹在 0.5~2.0 倍）。"
            ),
        )
        tick_interval: int = Field(
            default=60,
            description="内驱后台演化间隔（秒），只推进内存中的活跃会话，不产生 IO。",
        )

        @field_validator("modulation_strength", mode="after")
        @classmethod
        def _clamp_modulation(cls, value: float) -> float:
            return max(0.0, min(float(value), 1.0))

        @field_validator("tick_interval", mode="after")
        @classmethod
        def _positive_tick(cls, value: int) -> int:
            return max(10, int(value))

    @config_section("appraisal")
    class AppraisalSection(SectionBase):
        """S1 感知评估配置（System 1：每批消息的结构化第一反应）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用 S1 感知评估。每批新消息先用轻量模型产出结构化评估："
                "情绪反应、登记簿归属（现实/故事）、场景/剧情事实、话题钩子、"
                "承诺、自然延迟建议。失败时静默跳过，不影响主流程。"
            ),
        )
        model_task: str = Field(
            default="sub_actor",
            description="S1 评估使用的模型任务名（建议轻量模型）。",
        )
        timeout_seconds: float = Field(
            default=8.0,
            description="S1 评估的超时秒数，超时视为本轮无评估。",
        )
        defer_enabled: bool = Field(
            default=True,
            description=(
                "是否启用 S1 自然延迟：评估建议'缓一缓再回'时，该意见在"
                "下一批消息的决策请求发起前生效（让出建议的等待秒数），"
                "不干扰当前轮的回复——S1 是副决策，永不阻塞主模型。"
            ),
        )
        max_defer_seconds: float = Field(
            default=18.0,
            description="单次自然延迟的上限（秒）。",
        )
        min_input_chars: int = Field(
            default=8,
            description="消息文本短于此长度时跳过 S1 评估（省钱，按消息本体字符数计量）。",
        )

        @field_validator("timeout_seconds", "max_defer_seconds", mode="after")
        @classmethod
        def _positive_seconds(cls, value: float) -> float:
            return max(0.5, float(value))

    @config_section("character")
    class CharacterSection(SectionBase):
        """角色卡配置（三层人设：红线 + 隐藏事实 + 剧情覆层）。"""

        redlines: list[str] = Field(
            default_factory=list,
            description=(
                "角色的行为红线——无论什么情况都不会做的事。"
                "例如：['不会发语音', '不会讨论政治话题']。"
            ),
        )
        hidden_facts: list[dict[str, str]] = Field(
            default_factory=list,
            description=(
                "角色的隐藏事实（秘密/过去/真实想法）。平时不进提示词，"
                "S1 判定揭示条件满足后才注入，角色因此可以欲言又止、有秘密可揭。"
                "每条格式：{fact: '事实内容', condition: '揭示条件（自然语言）'}。"
                "例：{fact: '其实早就知道Ta换了头像', condition: 'Ta 提到换头像的事'}。"
            ),
        )

    @config_section("beliefs")
    class BeliefsSection(SectionBase):
        """信念层配置（对用户/关系的持久化理解）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用信念固化。每次记忆压缩完成后，额外用轻量模型从"
                "近期事件中蒸馏对用户/关系的持久判断，渲染为'你已经知道的'块。"
            ),
        )
        model_task: str = Field(
            default="sub_actor",
            description="信念蒸馏使用的模型任务名。",
        )
        max_extract: int = Field(
            default=5,
            description="每次蒸馏最多提取的信念条数。",
        )

    @config_section("intent")
    class IntentSection(SectionBase):
        """主动意图队列配置。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用意图队列。话题钩子/到期承诺/内驱冲动以冲动值竞争，"
                "最强者越过阈值即触发主动发起（有目的的主动）。"
                "无合格意图时回退旧的沉默概率兜底。"
            ),
        )
        fire_threshold: float = Field(
            default=0.75,
            description="意图触发阈值（0~1），冲动值达到后允许触发。",
        )

        @field_validator("fire_threshold", mode="after")
        @classmethod
        def _clamp_threshold(cls, value: float) -> float:
            return max(0.1, min(float(value), 1.0))

    @config_section("memo")
    class MemoSection(SectionBase):
        """备忘录配置（LLM 显式中短期便签）。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否启用备忘录。启用后模型可调用 nfc_memo / nfc_memo_delete "
                "给自己记带过期时间的便签，便签渲染进每轮提示词末尾"
                "（turn 级，不进对话链）。关闭则动作仍可注册但不再渲染。"
            ),
        )
        max_entries: int = Field(
            default=10,
            description="单聊最大有效备忘条数，超出按创建时间淘汰最早一条。",
        )
        default_expire_hours: float = Field(
            default=24.0,
            description="模型未指定 expire_hours 时的默认存活时长（小时）。",
        )
        min_expire_hours: float = Field(
            default=1.0,
            description="单条备忘最短存活时长（小时）。",
        )
        max_expire_hours: float = Field(
            default=336.0,
            description="单条备忘最长存活时长（小时），默认 14 天。",
        )

        @field_validator("max_entries", mode="after")
        @classmethod
        def _clamp_entries(cls, value: int) -> int:
            return max(1, min(int(value), 50))

        @field_validator(
            "default_expire_hours",
            "min_expire_hours",
            "max_expire_hours",
            mode="after",
        )
        @classmethod
        def _positive_hours(cls, value: float) -> float:
            return max(0.1, float(value))

    @config_section("debug")
    class DebugSection(SectionBase):
        """调试配置。"""

        show_prompt: bool = Field(
            default=False,
            description="是否在日志中显示发送给 LLM 的完整提示词",
        )
        show_response: bool = Field(
            default=True,
            description="是否在日志中显示 LLM 响应的美化摘要",
        )

    general: GeneralSection = Field(default_factory=GeneralSection)
    wait: WaitSection = Field(default_factory=WaitSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    reply: ReplySection = Field(default_factory=ReplySection)
    prompt: PromptSection = Field(default_factory=PromptSection)
    buffer: BufferSection = Field(default_factory=BufferSection)
    flashback: FlashbackSection = Field(default_factory=FlashbackSection)
    drives: DrivesSection = Field(default_factory=DrivesSection)
    appraisal: AppraisalSection = Field(default_factory=AppraisalSection)
    character: CharacterSection = Field(default_factory=CharacterSection)
    beliefs: BeliefsSection = Field(default_factory=BeliefsSection)
    intent: IntentSection = Field(default_factory=IntentSection)
    memo: MemoSection = Field(default_factory=MemoSection)
    debug: DebugSection = Field(default_factory=DebugSection)
