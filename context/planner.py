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
    ) -> ContextPlan:
        """规划本轮用户输入和第三方上下文贡献。

        将收集到的贡献按 scope 分为：
        - session: 缓存复用，内容变更时静默更新
        - turn: 每轮独立
        """
        user_text = f"[新消息]\n{formatted_unreads}"
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

        return ContextPlan(
            user_text=user_text,
            contributions=turn_contributions,
            session_contributions=session_contributions,
        )