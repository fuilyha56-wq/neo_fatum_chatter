"""NeoFatumChatter 配置定义。

定义插件所有可配置参数，基于 Pydantic + TOML 热重载。
通过 @config_section 划分为语义清晰的 Section。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import field_validator

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class NFCConfig(BaseConfig):
    """NeoFatumChatter 配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "NeoFatumChatter 配置"

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
            description="主动发起和超时上下文是否使用工具调用决策提示，默认启用",
        )
        max_compat_retries: int = Field(
            default=1,
            description=(
                "纯文本感知草稿未形成工具调用时的最大重试次数。"
                "NFC 会把该轮输出视为未发送草稿，并注入 tool-call 约束后重试。"
                "0 表示不重试。"
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
            default=["send_text", "pass_and_wait", "stop_conversation"],
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

        def apply_rules(self, raw_seconds: float, consecutive_timeouts: int) -> float:
            """应用等待时长规则。raw_seconds <= 0 或 enabled=false 时返回 0。"""
            if not self.enabled or raw_seconds <= 0:
                return 0.0
            if consecutive_timeouts >= self.max_consecutive_timeouts:
                return 0.0
            return max(self.min_seconds, min(raw_seconds, self.max_seconds))

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
        streaming_enabled: bool = Field(
            default=False,
            description=(
                "是否启用流式回复（打字机效果）。启用后，长消息会分块逐步发送，"
                "模拟真人边打字边发送的体验。需要平台适配器支持编辑消息。"
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

    @config_section("prompt")
    class PromptSection(SectionBase):
        """提示词配置。"""

        max_log_entries: int = Field(
            default=50, description="最大活动流条目数"
        )
        max_context_payloads: int = Field(
            default=20, description="LLM 上下文持久化链最大条目数（超出时裁剪最旧的 USER/ASSISTANT 对）"
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

        @field_validator("accumulate_window", "accumulate_max_window", "interrupt_poll_seconds", mode="after")
        @classmethod
        def _clamp_non_negative(cls, value: float) -> float:
            """将消息缓冲与轮询时间规整为非负数。"""
            return max(0.0, float(value))

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

    @config_section("group")
    class GroupSection(SectionBase):
        """群聊模式配置（启用后群聊走 DFC 风格状态机，私聊保持心理活动流不变）。"""

        enabled: bool = Field(
            default=False,
            description=(
                "是否启用群聊模式。"
                "启用后，遇到群聊会走独立的 DFC 风格状态机（send_text/pass_and_wait/stop_conversation），"
                "私聊保持原有的心理活动流模式不变。"
            ),
        )
        response_mode: str = Field(
            default="sub_agent",
            description=(
                "群聊响应模式："
                "sub_agent—使用子代理判定是否回复(默认)；"
                "always—收到任何消息都回复；"
                "mention_only—只在被 @ 时回复。"
            ),
        )
        base_response_probability: float = Field(
            default=0.1,
            description="sub_agent 模式下，本地概率直通响应的基础概率",
        )
        mention_bonus: float = Field(
            default=0.7,
            description="未读消息中提到 bot 名字时，本地直通响应概率的加成",
        )
        alias_bonus: float = Field(
            default=0.4,
            description="未读消息中提到 bot 别名时，本地直通响应概率的加成",
        )
        unread_message_bonus: float = Field(
            default=0.05,
            description="每条未读消息累加到本地直通响应概率的加成（与未读条数相乘）",
        )
        next_tick_reply_bonus: float = Field(
            default=0.15,
            description="刚回复后下一次 tick 的直通响应概率加成（保证连续对话不中断）",
        )
        cooldown_minutes: float = Field(
            default=5.0,
            description="stop_conversation 默认冷却时长(分钟)，可被工具调用参数覆盖",
        )
        enable_stop_direct_message_wake: bool = Field(
            default=False,
            description=(
                "stop_conversation 冷却期内，被 @ 或私下点名的消息是否能唤醒 bot 提前结束冷却。"
            ),
        )
        stop_direct_message_wake_probability: float = Field(
            default=0.5,
            description="冷却唤醒概率，0-1 之间",
        )
        enable_cooldown: bool = Field(
            default=True,
            description="stop_conversation 的冷却时间是否真正生效。关闭后 stop 只结束本轮，不产生冷却。",
        )
        enable_action_suspend: bool = Field(
            default=True,
            description="当本轮全部工具调用都是 action 时，是否注入 __SUSPEND__ 并立即挂起等待。关闭后 action-only 回合将进入 FOLLOW_UP 继续推理。",
        )
        enable_programmatic_controller: bool = Field(
            default=True,
            description="是否启用本地概率门。关闭后 sub_agent 模式下跳过本地概率直通，始终交给 LLM sub-agent 判定。",
        )
        native_multimodal: bool = Field(
            default=False,
            description="群聊路径是否启用原生多模态。启用后图片直接打包进 LLM payload。",
        )
        reinforce_negative_behaviors: bool = Field(
            default=True,
            description="是否在每轮 user prompt 的 extra 区块中再次强调负面行为约束。",
        )
        enable_llm_stream: bool = Field(
            default=False,
            description="群聊 LLM 请求是否启用流式响应。",
        )
        max_context_groups: int = Field(
            default=8,
            description=(
                "群聊路径保留的最近 QA 组数。每组对应一次 USER→ASSISTANT(可能含 tool_call→tool_result)→... 完整对话。"
                "群聊状态机跨轮持有同一个 LLMResponse；不裁剪时 payload 会持续累积，"
                "上下文逼近模型 max_context 时可能触发 Connection error / 服务端拒大请求。"
                "设为 0 关闭裁剪（不推荐）。"
            ),
        )

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """群聊提示词覆盖配置（仅群聊路径生效；空字符串表示使用内置默认值）。"""

        private_theme_guide: str = Field(
            default="",
            description=(
                "私聊场景引导（注入到群聊系统提示词的 theme_guide 占位符；"
                "私聊路径已有自己的提示词体系，不读取此项）。"
            ),
        )
        group_theme_guide: str = Field(
            default="",
            description="群聊场景引导（注入到群聊系统提示词的 theme_guide 占位符）",
        )
        custom_decision_prompt: str = Field(
            default="",
            description=(
                "群聊系统提示词中的 custom_rules 区块（覆盖默认值）。"
                "留空则使用内置默认提示词。"
            ),
        )
        sub_agent_prompt: str = Field(
            default="",
            description=(
                "群聊 sub-agent 决策系统提示词（支持 {nickname} {bot_id} {bot_id_section} 占位符）。"
                "留空则使用内置默认值。"
            ),
        )
        segment_instruction: str = Field(
            default="",
            description=(
                "群聊场景下的消息分段指引（注入到系统提示词）。"
                "留空则不注入。"
            ),
        )
        wait_instruction: str = Field(
            default="",
            description=(
                "群聊场景下 max_wait_seconds 的指引文本。"
                "留空则不注入（群聊主路径默认不使用 max_wait_seconds）。"
            ),
        )

    general: GeneralSection = Field(default_factory=GeneralSection)
    wait: WaitSection = Field(default_factory=WaitSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    reply: ReplySection = Field(default_factory=ReplySection)
    prompt: PromptSection = Field(default_factory=PromptSection)
    buffer: BufferSection = Field(default_factory=BufferSection)
    debug: DebugSection = Field(default_factory=DebugSection)
    group: GroupSection = Field(default_factory=GroupSection)
    prompts: PromptsSection = Field(default_factory=PromptsSection)
