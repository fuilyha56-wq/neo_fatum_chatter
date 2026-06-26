"""NFC 上下文渲染器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from src.core.prompt import get_prompt_manager
from src.kernel.llm import Content, LLMPayload, ROLE, Text

from .sources.history_source import (
    build_current_time_payload,
    build_fused_narrative as build_history_narrative,
    restore_chain_payloads as restore_history_chain_payloads,
)
from .types import ContextContribution, ContextPlan, InitialContextPlan

if TYPE_CHECKING:
    from src.core.models.stream import ChatStream


class ContextRenderer:
    """负责把 ContextPlan 和历史状态渲染成 LLM payload。"""

    _OWNER_RENDER_ORDER: tuple[str, ...] = (
        "policy",
        "self_state",
        "user_state",
        "relationship_state",
        "scene_evidence",
        "notice",
    )
    _OWNER_SECTION_TITLES: dict[str, str] = {
        "policy": "[策略约束]",
        "self_state": "[你的状态]",
        "user_state": "[对方状态]",
        "relationship_state": "[关系状态]",
        "scene_evidence": "[场景证据]",
    }

    async def render_initial_context(
        self,
        *,
        chat_stream: ChatStream,
        plan: InitialContextPlan,
        mental_log: Any,
        serialized_chain_payloads: list[dict[str, Any]],
        session: Any | None = None,
        build_system_prompt_fn: Callable[
            [ChatStream, dict[str, Any] | None], Awaitable[str]
        ]
        | None = None,
        build_fused_narrative_fn: Callable[[ChatStream, Any, float | None], str]
        | None = None,
    ) -> tuple[list[LLMPayload], bool]:
        """渲染 execute 启动时需要的初始 payload 列表。

        设计要点：只有稳定的系统提示词放入 SYSTEM payload，
        所有动态内容（summary/narrative/time/channel）合并到一个 USER payload，
        以最大化 LLM prompt prefix cache 命中率。
        """
        system_prompt_builder = build_system_prompt_fn or self.build_system_prompt
        fused_narrative_builder = (
            build_fused_narrative_fn or self.build_fused_narrative
        )

        # 1. 稳定系统提示词 → SYSTEM payload（prefix cache 锚点）
        system_prompt = await system_prompt_builder(
            chat_stream,
            plan.system_extra_vars,
        )
        system_payloads: list[LLMPayload] = [LLMPayload(ROLE.SYSTEM, Text(system_prompt))]

        # 2. 收集所有动态内容，合并到一个 USER payload
        dynamic_parts: list[str] = []

        # 平台/通道信息
        channel_text = self._build_channel_text(chat_stream)
        if channel_text:
            dynamic_parts.append(channel_text)

        # 近期记忆摘要
        summary_text = self._build_summary_text(chat_stream, plan.history_summary)
        if summary_text:
            dynamic_parts.append(summary_text)

        # 融合叙事
        raw_limits = getattr(session, "_nfc_context_limits", None)
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        chain_limit = int(limits.get("max_initial_chain_payloads", 0) or 0)
        narrative_limit = int(limits.get("max_fused_narrative_chars", 0) or 0)

        history_text = self._limit_fused_narrative(
            self._get_or_build_frozen_narrative(
                chat_stream=chat_stream,
                mental_log=mental_log,
                before_ts=plan.history_before_ts,
                session=session,
                fused_narrative_builder=fused_narrative_builder,
            ),
            max_chars=narrative_limit,
        )
        if history_text:
            dynamic_parts.append(history_text)
        else:
            # 无历史时至少显示当前日期
            time_payload = build_current_time_payload()
            for item in time_payload.content:
                if hasattr(item, "text"):
                    dynamic_parts.append(item.text)  # type: ignore[attr-defined]

        # 合并动态内容为一个 USER payload
        chain_payloads: list[LLMPayload] = []
        if dynamic_parts:
            merged_dynamic_text = "\n\n---\n\n".join(dynamic_parts)
            chain_payloads.append(LLMPayload(ROLE.USER, Text(merged_dynamic_text)))

        # 3. 恢复历史对话链（独立 payload，绕过 context manager 避免重复注入 system_reminder）
        limited_chain = self._limit_serialized_chain(
            serialized_chain_payloads,
            max_payloads=chain_limit,
        )
        restored_payloads = restore_history_chain_payloads(limited_chain)
        chain_payloads.extend(restored_payloads)

        # 4. 动态状态（场景/主动发起等）
        dynamic_context = plan.dynamic_context.strip()
        if dynamic_context:
            chain_payloads.append(
                LLMPayload(
                    ROLE.USER,
                    Text(f"【当前动态状态】\n{dynamic_context}"),
                )
            )

        # 组装最终列表：system_payloads + chain_payloads
        payloads = system_payloads + chain_payloads
        has_history = bool(history_text) or bool(restored_payloads)
        return payloads, has_history

    async def build_system_prompt(
        self,
        chat_stream: ChatStream,
        extra_vars: dict[str, Any] | None = None,
    ) -> str:
        """构建稳定系统提示词。

        NFC 的系统提示词是自动前缀缓存的核心锚点，不能发布
        ``on_prompt_build`` 事件给第三方动态注入器修改。动态上下文统一
        通过 ``nfc_user_prompt`` 的 ``context_contributions`` 注入。
        """
        from ..prompts.modules import build_mental_log_hint

        pm = get_prompt_manager()
        tmpl = pm.get_template("NFC_system_prompt")
        if not tmpl:
            return ""

        tmpl = tmpl.clone()
        tmpl.set("platform", chat_stream.platform or "unknown")
        tmpl.set("chat_type", str(chat_stream.chat_type or "unknown"))
        tmpl.set("bot_id", chat_stream.bot_id or "")
        tmpl.set("stream_id", str(chat_stream.stream_id or ""))
        tmpl.set("mental_log_hint", build_mental_log_hint())
        tmpl.set("theme_guide", self._get_theme_guide(chat_stream))

        if extra_vars:
            for key, value in extra_vars.items():
                tmpl.set(key, value)

        # 绕过 on_prompt_build 事件：NFC 系统提示词不允许第三方注入修改，
        # 以保护前缀缓存稳定性。动态上下文统一走 context_contributions 机制。
        return tmpl._render(  # noqa: SLF001
            tmpl.template,
            dict(tmpl.values),
            dict(tmpl.policies),
            strict=False,
        )

    def render_user_payload(
        self,
        plan: ContextPlan,
        media_items: list[Any] | None = None,
    ) -> tuple[LLMPayload, LLMPayload | None]:
        """将 ContextPlan 渲染为用户 payload。"""
        content: Content | list[Content]
        if media_items:
            from ..multimodal import build_multimodal_content

            content = build_multimodal_content(plan.user_text, media_items)
        else:
            content = Text(plan.user_text)

        user_payload = LLMPayload(ROLE.USER, content)  # type: ignore[arg-type]
        extra_payload = self._render_combined_contributions(
            session_contributions=plan.session_contributions,
            turn_contributions=plan.contributions,
        )
        return user_payload, extra_payload

    def _render_combined_contributions(
        self,
        session_contributions: list[ContextContribution],
        turn_contributions: list[ContextContribution],
    ) -> LLMPayload | None:
        """合并 session 级和 turn 级贡献为一个临时 USER payload。

        session 贡献在前（内容稳定，利于缓存扩展），turn 贡献在后。
        """
        parts: list[str] = []

        # Session 级贡献（稳定部分）
        session_text = self._render_scoped_contributions(
            session_contributions, label="[持久上下文]"
        )
        if session_text:
            parts.append(session_text)

        # Turn 级贡献（每轮变化）
        turn_text = self._render_scoped_contributions(
            turn_contributions, label="[附加上下文]"
        )
        if turn_text:
            parts.append(turn_text)

        if not parts:
            return None

        return LLMPayload(ROLE.USER, Text("\n\n".join(parts)))

    def _render_scoped_contributions(
        self,
        contributions: list[ContextContribution],
        label: str,
    ) -> str:
        """渲染某一 scope 的贡献列表为带标签的文本块。"""
        valid = [c for c in contributions if c.content.strip()]
        if not valid:
            return ""

        joined_contents = "\n\n".join(
            block
            for owner in self._OWNER_RENDER_ORDER
            if (block := self._render_owner_contribution_block(owner, valid))
        )
        if not joined_contents:
            return ""

        return f"{label}\n{joined_contents}"

    def _render_owner_contribution_block(
        self,
        owner: str,
        contributions: list[ContextContribution],
    ) -> str:
        """按 owner 渲染 turn contribution 分区。"""
        owner_contributions = sorted(
            (
                contribution
                for contribution in contributions
                if contribution.owner == owner
            ),
            key=lambda contribution: (
                -contribution.priority,
                contribution.source,
                contribution.content,
            ),
        )
        rendered_contents = [
            rendered
            for rendered in (
                self._render_contribution_text(contribution)
                for contribution in owner_contributions
            )
            if rendered
        ]
        if not rendered_contents:
            return ""

        joined_contents = "\n\n".join(rendered_contents)
        section_title = self._OWNER_SECTION_TITLES.get(owner, "")
        if not section_title:
            return joined_contents
        return f"{section_title}\n{joined_contents}"

    @staticmethod
    def _build_channel_text(chat_stream: Any) -> str:
        """构建平台/通道上下文文本。"""
        platform = getattr(chat_stream, "platform", "unknown") or "unknown"
        chat_type = str(getattr(chat_stream, "chat_type", "unknown") or "unknown")
        bot_id = getattr(chat_stream, "bot_id", "") or ""
        parts = [f"平台: {platform}", f"聊天类型: {chat_type}"]
        if bot_id:
            parts.append(f"你的ID: {bot_id}")
        parts.append("注意：不要凭空臆测物理场景细节（如对方在做什么、环境怎样），除非对方明确提到。")
        return "\n".join(parts)

    @staticmethod
    def _build_summary_text(chat_stream: Any, history_summary: str) -> str:
        """构建近期记忆摘要文本。"""
        summary = history_summary.strip()
        if not summary:
            return ""
        user_name = (
            getattr(chat_stream, "partner_name", None)
            or getattr(chat_stream, "group_name", None)
            or "对方"
        )
        return f"【你对{user_name}的近期记忆】\n{summary}"

    @staticmethod
    def restore_chain_payloads(
        serialized_chain_payloads: list[dict[str, Any]],
    ) -> list[LLMPayload]:
        """从序列化的 USER/ASSISTANT pair 恢复 payload。"""
        return restore_history_chain_payloads(serialized_chain_payloads)

    @staticmethod
    def _limit_serialized_chain(
        serialized_chain_payloads: list[dict[str, Any]],
        *,
        max_payloads: int,
    ) -> list[dict[str, Any]]:
        """限制本次恢复进 LLM 的 chain 条数，并保证首条为 user。"""
        if max_payloads > 0:
            limited = list(serialized_chain_payloads[-max_payloads:])
        else:
            limited = list(serialized_chain_payloads)
        while limited and limited[0].get("role") != "user":
            limited.pop(0)
        return limited

    @staticmethod
    def _limit_fused_narrative(history_text: str, *, max_chars: int) -> str:
        """限制融合叙事长度，保留最近部分。"""
        if max_chars <= 0 or len(history_text) <= max_chars:
            return history_text
        tail = history_text[-max_chars:]
        newline_index = tail.find("\n")
        if newline_index >= 0:
            tail = tail[newline_index + 1:]
        return "（较早的融合叙事已省略，仅保留最近部分）\n" + tail

    def _get_or_build_frozen_narrative(
        self,
        *,
        chat_stream: ChatStream,
        mental_log: Any,
        before_ts: float | None,
        session: Any | None,
        fused_narrative_builder: Callable[[ChatStream, Any, float | None], str],
    ) -> str:
        """复用或生成融合叙事。

        NFC 会在 Wait/Stop 后跨 execute() 重建 payload。若每次都重新扫描
        MentalLog 与历史消息，叙事尾部可能随运行时状态发生细微变化，从而破坏
        LLM 服务端 prompt prefix cache。这里按 before_ts（即 chain_cutoff_ts）
        冻结叙事文本，使相同截止点的重建得到字节级一致的前缀。
        """
        if session is None:
            return fused_narrative_builder(chat_stream, mental_log, before_ts)

        cutoff = float(before_ts or 0.0)
        frozen = str(getattr(session, "frozen_narrative", "") or "")
        frozen_cutoff = float(getattr(session, "frozen_narrative_cutoff_ts", 0.0) or 0.0)
        if frozen and frozen_cutoff == cutoff:
            return frozen

        history_text = fused_narrative_builder(chat_stream, mental_log, before_ts)
        setattr(session, "frozen_narrative", history_text)
        setattr(session, "frozen_narrative_cutoff_ts", cutoff)
        return history_text

    @staticmethod
    def _render_contribution_text(contribution: ContextContribution) -> str:
        """渲染单条上下文贡献文本。"""
        content = contribution.content.strip()
        if not content:
            return ""

        if contribution.source == "legacy.on_prompt_build.extra":
            if content.startswith("[SYSTEM REMINDER]"):
                return content
            return f"[SYSTEM REMINDER]\n{content}"

        return content

    @staticmethod
    def _get_theme_guide(chat_stream: ChatStream) -> str:
        """根据聊天类型返回场景引导文本。"""
        _ = chat_stream
        return ""

    @staticmethod
    def build_fused_narrative(
        chat_stream: ChatStream,
        mental_log: Any,
        before_ts: float | None = None,
    ) -> str:
        """构建聊天历史与内心独白的融合叙事。"""
        return build_history_narrative(
            chat_stream,
            mental_log,
            before_ts=before_ts,
        )