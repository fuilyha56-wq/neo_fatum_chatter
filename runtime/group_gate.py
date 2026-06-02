"""NFC 群聊门控模块。

根据 ``config.group.response_mode`` 和本地概率计算决定是否应答：

- ``always`` → 直接 respond
- ``mention_only`` → 仅 @bot_id 时 respond
- ``sub_agent`` → 概率门 + LM 判定

概率门逻辑照搬 DFC ``_compute_sub_agent_bypass_probability``：
    base + 名字命中(+mention_bonus) + 别名(+alias_bonus) + 未读数×unread_message_bonus + next_tick bonus
    random < probability → 直通 respond
    否则 → 调用 ``group_decision.decide_group_response()``
    异常时默认 respond
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.config import get_core_config

from ..actions.send_text import consume_next_tick_sub_agent_bonus

if TYPE_CHECKING:
    from src.core.models.message import Message
    from src.core.models.stream import ChatStream

    from ..config import NFCConfig

logger = get_logger("NFC_group_gate")


def _get_identity_names(chat_stream: "ChatStream") -> tuple[str, list[str]]:
    """获取 bot 名字与别名，供概率门做关键词匹配。"""
    fallback_nickname = (
        chat_stream.bot_nickname.strip()
        if isinstance(getattr(chat_stream, "bot_nickname", None), str)
        else ""
    )
    try:
        personality = get_core_config().personality
    except RuntimeError:
        return fallback_nickname, []

    nickname = (
        personality.nickname.strip()
        if isinstance(personality.nickname, str) and personality.nickname.strip()
        else fallback_nickname
    )
    alias_names = [
        alias.strip()
        for alias in personality.alias_names
        if isinstance(alias, str) and alias.strip()
    ]
    return nickname, alias_names


def _message_text(msg: "Message") -> str:
    """提取消息可搜索文本。"""
    if isinstance(getattr(msg, "processed_plain_text", None), str) and msg.processed_plain_text:
        return msg.processed_plain_text
    if isinstance(getattr(msg, "content", None), str):
        return msg.content
    return str(getattr(msg, "content", ""))


def _messages_contain_any_name(
    unread_msgs: list["Message"],
    names: list[str],
) -> bool:
    """判断任意未读消息是否包含指定名字。"""
    normalized = [n.lower() for n in names if n]
    if not normalized:
        return False
    for msg in unread_msgs:
        text = _message_text(msg).lower()
        if any(n in text for n in normalized):
            return True
    return False


def _is_directly_mentioned(
    unread_msgs: list["Message"],
    bot_id: str,
) -> bool:
    """检查未读消息中是否存在直接 @bot_id 的行为。"""
    if not bot_id:
        return False
    for msg in unread_msgs:
        text = _message_text(msg)
        # 常见格式：[CQ:at,qq=123456] 或 @123456
        if f"qq={bot_id}" in text or f"@{bot_id}" in text:
            return True
        # 某些平台的 extra 字段
        extra = getattr(msg, "extra", None) or {}
        at_list = extra.get("at_list") or extra.get("at_users") or []
        if isinstance(at_list, list) and bot_id in [str(a) for a in at_list]:
            return True
    return False


def _compute_bypass_probability(
    unread_msgs: list["Message"],
    chat_stream: "ChatStream",
    config: "NFCConfig",
) -> tuple[float, str]:
    """计算本地概率直通值。"""
    group_cfg = config.group
    nickname, alias_names = _get_identity_names(chat_stream)

    probability = group_cfg.base_response_probability
    reasons = [f"基础概率 {probability:.2f}"]

    if nickname and _messages_contain_any_name(unread_msgs, [nickname]):
        probability += group_cfg.mention_bonus
        reasons.append(f"命中名字 +{group_cfg.mention_bonus:.2f}")

    if _messages_contain_any_name(unread_msgs, alias_names):
        probability += group_cfg.alias_bonus
        reasons.append(f"命中别名 +{group_cfg.alias_bonus:.2f}")

    unread_bonus = len(unread_msgs) * group_cfg.unread_message_bonus
    if unread_bonus > 0:
        probability += unread_bonus
        reasons.append(f"{len(unread_msgs)} 条未读 +{unread_bonus:.2f}")

    next_tick_bonus = consume_next_tick_sub_agent_bonus(chat_stream)
    if next_tick_bonus > 0:
        probability += next_tick_bonus
        reasons.append(f"上次回复后的下一 tick +{next_tick_bonus:.2f}")

    capped = min(probability, 1.0)
    if capped != probability:
        reasons.append("封顶 1.00")

    return capped, "，".join(reasons)


async def should_respond_in_group(
    chatter: Any,
    unread_msgs: list["Message"],
    unreads_text: str,
    chat_stream: "ChatStream",
    config: "NFCConfig",
) -> bool:
    """群聊门控：判定本次 tick 是否应该执行 LLM 回复。

    Args:
        chatter: NeoFatumChatter 实例
        unread_msgs: 当前未读消息列表
        unreads_text: 格式化后的未读消息文本（供 sub-agent 使用）
        chat_stream: 当前聊天流
        config: NFC 配置

    Returns:
        bool: True 表示应该响应，False 表示跳过
    """
    mode = config.group.response_mode.strip().lower()

    # ── always 模式 ──
    if mode == "always":
        logger.debug("群聊门控: always 模式，直接响应")
        return True

    # ── mention_only 模式 ──
    bot_id = str(chat_stream.bot_id or "")
    if mode == "mention_only":
        mentioned = _is_directly_mentioned(unread_msgs, bot_id)
        logger.debug(f"群聊门控: mention_only 模式, 被 @ = {mentioned}")
        return mentioned

    # ── sub_agent 模式（默认） ──

    # 1) 直接 @bot_id → 必定 respond
    if _is_directly_mentioned(unread_msgs, bot_id):
        logger.info("群聊门控: 检测到直接 @bot，必定响应")
        return True

    # 2) 本地概率直通（仅在 enable_programmatic_controller 启用时）
    if config.group.enable_programmatic_controller:
        probability, reason = _compute_bypass_probability(unread_msgs, chat_stream, config)
        roll = random.random()
        if roll < probability:
            logger.info(f"群聊门控: 概率直通响应 (roll={roll:.3f} < {probability:.3f}; {reason})")
            return True

        logger.debug(f"群聊门控: 概率未直通 (roll={roll:.3f} >= {probability:.3f}; {reason})，进入 sub-agent 判定")
    else:
        logger.debug("群聊门控: enable_programmatic_controller 关闭，跳过本地概率门，直接进入 sub-agent 判定")

    # 3) Sub-agent LM 判定
    from ..protocol.group_decision import decide_group_response

    try:
        decision = await decide_group_response(
            chatter=chatter,
            unreads_text=unreads_text,
            chat_stream=chat_stream,
            config=config,
        )
        logger.info(f"群聊门控: sub-agent 决策 = {decision['should_respond']} ({decision['reason']})")
        return decision["should_respond"]
    except Exception as error:
        logger.error(f"群聊门控: sub-agent 决策异常，默认响应: {error}", exc_info=True)
        return True
