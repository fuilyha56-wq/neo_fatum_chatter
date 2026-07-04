"""NFC 上下文规划器。"""

from __future__ import annotations

import hashlib
from typing import Any

from .sources.initial_source import build_initial_context_plan
from .sources.plugin_source import collect_plugin_turn_contributions
from .types import ContextContribution, ContextPlan, InitialContextPlan


def _filter_duplicate_turn_contributions(
    formatted_unreads: str,
    contributions: list[ContextContribution],
) -> list[ContextContribution]:
    """过滤已由上游附加到本轮消息中的同内容上下文。"""

    if not formatted_unreads or not contributions:
        return contributions

    return [
        contribution
        for contribution in contributions
        if contribution.content not in formatted_unreads
    ]


def _compute_contributions_hash(contributions: list[ContextContribution]) -> str:
    """计算贡献列表的内容哈希，用于判断是否变更。"""
    parts = sorted(
        f"{c.source}:{c.owner}:{c.priority}:{c.content}"
        for c in contributions
    )
    return hashlib.md5("|".join(parts).encode()).hexdigest()


class ContextPlanner:
    """负责把单轮输入转换成结构化上下文计划。"""

    def __init__(self) -> None:
        # session scope 贡献缓存
        self._session_contributions_cache: list[ContextContribution] = []
        self._session_contributions_hash: str = ""

    def plan_initial_context(
        self,
        *,
        chat_stream: Any,
        config: Any,
        session: Any,
    ) -> InitialContextPlan:
        """规划 execute 启动时所需的初始上下文数据。"""
        return build_initial_context_plan(
            chat_stream=chat_stream,
            config=config,
            session=session,
        )

    async def plan_user_turn(
        self,
        *,
        formatted_unreads: str,
        stream_id: str = "",
        session: Any = None,
    ) -> ContextPlan:
        """规划本轮用户输入和第三方上下文贡献。

        将收集到的贡献按 scope 分为：
        - session: 缓存复用，内容变更时静默更新
        - turn: 每轮独立

        主动思考触发时，ProactiveHandler 把富上下文写入 session.pending_proactive_context，
        此处读取后作为 turn contribution（owner=notice）注入到 transient extra_payload，
        避免动态内容写入 user_text 破坏 LLM prompt prefix cache。触发消息 content 使用
        稳定占位符 ``[proactive_trigger]``，从 user_text 移除后只剩稳定结构。
        """
        # 主动思考富上下文：从 session 读取并清空，作为 turn contribution 注入
        proactive_context = ""
        if session is not None:
            proactive_context = str(getattr(session, "pending_proactive_context", "") or "")
            if proactive_context:
                try:
                    session.pending_proactive_context = ""
                except Exception:
                    pass

        # 若本轮是主动思考触发，从 formatted_unreads 中移除占位符行，使 user_text 稳定
        cleaned_unreads = formatted_unreads
        if proactive_context and "[proactive_trigger]" in formatted_unreads:
            lines = formatted_unreads.splitlines()
            kept = [line for line in lines if "[proactive_trigger]" not in line]
            cleaned_unreads = "\n".join(kept).strip()

        user_text = (
            f"[新消息]\n{cleaned_unreads}"
            "\n\n---\n重申：你的响应必须仅包含工具调用（nfc_reply 或 do_nothing），不要在文本区域输出任何内容。"
        )
        all_contributions = await collect_plugin_turn_contributions(
            prompt_name="NFC_user_prompt",
            content=user_text,
            stream_id=stream_id,
        )

        # 分离 session 和 turn scope
        session_raw = [c for c in all_contributions if c.scope == "session"]
        turn_raw = [c for c in all_contributions if c.scope != "session"]

        # session scope: hash 缓存，内容变更时静默接受
        if session_raw:
            new_hash = _compute_contributions_hash(session_raw)
            if new_hash != self._session_contributions_hash:
                self._session_contributions_cache = session_raw
                self._session_contributions_hash = new_hash
        # 若本轮未收到 session 贡献但缓存中有，保留缓存
        session_contributions = self._session_contributions_cache

        # turn scope: 去重过滤
        turn_contributions = _filter_duplicate_turn_contributions(
            formatted_unreads,
            turn_raw,
        )

        # 主动思考富上下文作为 turn contribution 注入（owner=notice, scope=turn）
        if proactive_context:
            turn_contributions.append(
                ContextContribution(
                    source="nfc.proactive_trigger",
                    owner="notice",
                    scope="turn",
                    priority=100,
                    ttl_turns=1,
                    content=proactive_context,
                )
            )

        return ContextPlan(
            user_text=user_text,
            contributions=turn_contributions,
            session_contributions=session_contributions,
        )