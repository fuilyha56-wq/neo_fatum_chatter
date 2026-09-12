"""NFC 私人备忘录动作。

提供 ``nfc_memo``（写入或刷新）与 ``nfc_memo_delete``（按 id 删除）
两个工具。备忘录定位为 LLM 显式标记的中短期关键事项——与 mental_log
（自动事件流）、history_summary（叙事压缩）、beliefs（消化后的持久
判断）互补，覆盖"接下来一段时间需要明确意识到的事"。

数据随会话持久化；渲染经 state_source 作为 turn 级上下文注入提示词
末尾，不进入对话链，因而不影响前缀缓存。
"""

from __future__ import annotations

import time
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..domain.memo import (
    MEMO_DEFAULT_EXPIRE_HOURS,
    MEMO_MAX_ENTRIES,
    MEMO_MAX_EXPIRE_HOURS,
    MEMO_MIN_EXPIRE_HOURS,
    Memo,
)
from ..models import NFCEventType

logger = get_logger("NFC_memo")


def _resolve_memo_limits(plugin: BaseAction) -> tuple[int, float, float, float]:
    """从插件配置读取备忘录边界。

    Returns:
        tuple: ``(max_entries, default_hours, min_hours, max_hours)``；
        配置不可用时回退 domain 常量。
    """
    config = getattr(plugin, "config", None)
    memo_cfg = getattr(config, "memo", None)
    if memo_cfg is not None:
        return (
            int(getattr(memo_cfg, "max_entries", MEMO_MAX_ENTRIES)),
            float(
                getattr(memo_cfg, "default_expire_hours", MEMO_DEFAULT_EXPIRE_HOURS)
            ),
            float(getattr(memo_cfg, "min_expire_hours", MEMO_MIN_EXPIRE_HOURS)),
            float(getattr(memo_cfg, "max_expire_hours", MEMO_MAX_EXPIRE_HOURS)),
        )
    return (
        MEMO_MAX_ENTRIES,
        MEMO_DEFAULT_EXPIRE_HOURS,
        MEMO_MIN_EXPIRE_HOURS,
        MEMO_MAX_EXPIRE_HOURS,
    )


class NFCMemoAction(BaseAction):
    """写入或刷新一条私人备忘录。"""

    action_name = "nfc_memo"
    action_description = (
        "给自己记一条带过期时间的私人备忘便签。备忘条目会自动渲染到你的"
        "提示词末尾，让你保持对它的意识，但不需要时刻提起或反复念叨——"
        "只在恰当的时机自然地用上。\n\n"
        "**用途定位（语义比较宽，不必拘泥）：**\n"
        "- 对方提到的待办、约定、需要兑现的承诺\n"
        "- 当前需要避开 / 留意的话题或情绪状态\n"
        "- 对当前关系状态的小观察\n"
        "- 未来想问对方的问题、想分享的事\n"
        "只要你觉得「过几个小时或几天后回看时还想知道这件事」，就可以记。\n\n"
        "**重要：** 当某条备忘对应的事情已经做了 / 兑现了 / 不再相关时，"
        "请主动使用 nfc_memo_delete 删除它，避免便签和实际状态对不上。"
        "过期时间只是兜底，不要依赖它。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        content: Annotated[
            str,
            "备忘的核心内容。建议简洁明了，几十字以内描述清楚要记的事。",
        ],
        intent: Annotated[
            str,
            "为什么记这条 / 未来看到时希望提醒自己什么。"
            "建议填写，让未来的你看到时更容易理解它的意义。",
        ] = "",
        expire_hours: Annotated[
            float,
            "备忘的存活时长（小时），超出范围会自动夹到边界。",
        ] = MEMO_DEFAULT_EXPIRE_HOURS,
        **_extra,
    ) -> tuple[bool, str]:
        """写入或刷新一条备忘录。"""
        if _extra:
            logger.debug(f"忽略 memo 未知参数: {sorted(_extra.keys())}")

        normalized_content = content.strip()
        if not normalized_content:
            return False, "content 不能为空"

        max_entries, default_hours, min_hours, max_hours = _resolve_memo_limits(
            self.plugin
        )
        try:
            hours = float(expire_hours)
        except (TypeError, ValueError):
            hours = default_hours
        if hours <= 0:
            hours = default_hours
        hours = max(min_hours, min(hours, max_hours))

        now = time.time()
        new_memo = Memo(
            content=normalized_content,
            intent=intent.strip(),
            expires_at=now + hours * 3600.0,
        )

        session_store = self.plugin.session_store
        stream_id = self.chat_stream.stream_id
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            saved_memo, is_new = session.memos.upsert(
                new_memo, max_entries=max_entries
            )
            session.add_memo_event(NFCEventType.MEMO_WRITTEN, saved_memo)
            await session_store.save(session)

        action_word = "已记下" if is_new else "已刷新"
        logger.info(
            f"{action_word}备忘 id={saved_memo.memo_id}，"
            f"过期={hours:g}h，content={saved_memo.content[:40]}"
        )
        return True, (
            f"{action_word}备忘（id={saved_memo.memo_id}，"
            f"约 {hours:g} 小时后过期）"
        )


class NFCMemoDeleteAction(BaseAction):
    """按 id 删除一条或多条备忘录。"""

    action_name = "nfc_memo_delete"
    action_description = (
        "删除一条或多条已不再需要的备忘录。\n"
        "**典型场景：你看到备忘录里某条事情你刚刚已经做了/兑现了/不再相关了，"
        "就主动调用此工具删掉它，避免脑门便签和实际状态对不上。**\n"
        "memo_ids：从备忘录显示中读取的 id 列表（每条备忘渲染时都会显示其 id）。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        memo_ids: Annotated[
            list[str],
            "要删除的备忘 id 列表。从备忘录渲染中读取（每条都会显示其 id）。",
        ],
        **_extra,
    ) -> tuple[bool, str]:
        """删除指定 id 的备忘。"""
        if _extra:
            logger.debug(f"忽略 memo_delete 未知参数: {sorted(_extra.keys())}")

        # 模型偶尔会传单个字符串而非列表
        if isinstance(memo_ids, str):
            target_ids = [memo_ids.strip()] if memo_ids.strip() else []
        elif isinstance(memo_ids, list):
            target_ids = [str(item).strip() for item in memo_ids if str(item).strip()]
        else:
            target_ids = []
        if not target_ids:
            return False, "memo_ids 不能为空"

        session_store = self.plugin.session_store
        stream_id = self.chat_stream.stream_id
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            deleted = session.memos.delete_by_ids(target_ids)
            for memo in deleted:
                session.add_memo_event(NFCEventType.MEMO_DELETED, memo)
            await session_store.save(session)

        if not deleted:
            logger.debug(f"未找到匹配的备忘 ids={target_ids}")
            return True, f"未找到匹配的备忘（请求 ids={target_ids}）"

        deleted_ids = [memo.memo_id for memo in deleted]
        logger.info(f"删除了 {len(deleted)} 条备忘 ids={deleted_ids}")
        return True, f"已删除 {len(deleted)} 条备忘"
