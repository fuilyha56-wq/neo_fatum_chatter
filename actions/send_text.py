"""NFC 群聊场景：send_text 动作。

设计目标：
- 群聊版本不携带 thought/mood/expected_reaction 等私聊心理活动参数；
- 兼容 DFC 的 ``content/reply_to/at`` 三参数语义；
- 发送成功后对当前 stream 写入下一 tick 的 sub-agent 概率加成；
- 仅在 ``chatter_allow=["neo_fatum_chatter"]`` 下可用。
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any
from uuid import uuid4

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction
from src.core.models.message import Message, MessageType

logger = get_logger("NFC_send_text")


# DFC 等价常量。集中放在这里以便维护，与门控/orchestrator 共享。
_SEND_TEXT_TYPING_DELAY_PER_CHAR = 0.5
_SEND_TEXT_TYPING_DELAY_MAX_SECONDS = 10.0
_SUB_AGENT_NEXT_TICK_REPLY_BONUS = 0.5
_SUB_AGENT_NEXT_TICK_BONUS_ATTR = "_nfc_group_next_tick_bonus"


def set_next_tick_sub_agent_bonus(chat_stream: Any, bonus: float) -> None:
    """在 chat_stream.context 上写入下一 tick 的 sub-agent 概率加成。"""
    context = getattr(chat_stream, "context", None)
    if context is None:
        return
    current_bonus = getattr(context, _SUB_AGENT_NEXT_TICK_BONUS_ATTR, 0.0)
    setattr(
        context,
        _SUB_AGENT_NEXT_TICK_BONUS_ATTR,
        max(float(current_bonus), float(bonus)),
    )


def consume_next_tick_sub_agent_bonus(chat_stream: Any) -> float:
    """读取并清空下一 tick 的 sub-agent 概率加成。"""
    context = getattr(chat_stream, "context", None)
    if context is None:
        return 0.0
    bonus = float(getattr(context, _SUB_AGENT_NEXT_TICK_BONUS_ATTR, 0.0))
    setattr(context, _SUB_AGENT_NEXT_TICK_BONUS_ATTR, 0.0)
    return bonus


class NFCSendTextAction(BaseAction):
    """发送一段文本消息（群聊场景使用）。"""

    action_name: str = "nfc_send_text"
    action_description: str = (
        "发送一段文本消息给对方。这是你唯一发送文本消息的方式。你可以一次调用多个 send_text "
        "来分多段回复，但每次调用必须提供你想说的话的纯文本，不要添加任何标记或格式。"
        "content 参数只能包含发送给用户的正文，严禁将行为理由、内心独白或格式说明混入 content。"
        "你可以使用 reply_to 参数引用某条历史消息；若不引用，可用 at 参数指定要 @ 的对象。"
        "本工具无法发送表情包等非文本内容。所有 @ 行为都应通过 at 参数传递，"
        "而不是直接写在文本里，以确保正确解析与发送。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]

    @staticmethod
    def _typing_delay_seconds(content: str) -> float:
        """根据文本长度估算发送前的打字等待时间。"""
        delay = len(content) * _SEND_TEXT_TYPING_DELAY_PER_CHAR
        return min(delay, _SEND_TEXT_TYPING_DELAY_MAX_SECONDS)

    async def _sleep_for_typing_delay(self, content: str) -> None:
        delay = self._typing_delay_seconds(content)
        if delay > 0:
            await asyncio.sleep(delay)

    def _mark_sub_agent_bonus_on_success(self, success: bool) -> None:
        """发送成功后提升下一 tick 的 sub-agent 直通概率。"""
        if success:
            set_next_tick_sub_agent_bonus(
                self.chat_stream,
                _SUB_AGENT_NEXT_TICK_REPLY_BONUS,
            )

    async def execute(
        self,
        content: Annotated[str, "要发送的文本内容；只填写你想说的话本体，不要添加任何标记"],
        reply_to: Annotated[str | None, "可选，要引用回复的目标消息 ID"] = None,
        at: Annotated[str | None, "可选，群聊中要 @ 的对象（QQ 号或昵称）；reply_to 存在时本字段被忽略"] = None,
    ):
        """执行发送文本消息的逻辑。

        Args:
            content: 要发送的纯文本
            reply_to: 引用某条历史消息（可选）
            at: 显式指定 @ 对象（可选；reply_to 存在时被忽略）
        """
        # 1) 清洗 LLM 可能侧漏的 reason 字段（兼容 DFC 行为）
        if content:
            content = re.split(r"[,，]?\s*reason[:：]", content, flags=re.IGNORECASE)[0].strip()

        # 2) 解析 content 开头的 @对象，并从正文中移除
        at_prefix_hint: str | None = None
        if content:
            at_match = re.match(r"^\s*@([^\s]+)\s*", content)
            if at_match:
                at_prefix_hint = at_match.group(1).strip()
                content = content[at_match.end():].lstrip()

        if not content:
            yield True, "内容为空，跳过发送"
            return

        chat_stream = self.chat_stream

        # 3) reply_to 分支：构造带 reply_to 的 Message
        if reply_to:
            target_stream_id = chat_stream.stream_id
            platform = chat_stream.platform
            chat_type = chat_stream.chat_type
            context = chat_stream.context

            from src.core.managers.adapter_manager import get_adapter_manager

            bot_info = await get_adapter_manager().get_bot_info_by_platform(platform)

            target_user_id = None
            target_group_id = None
            target_user_name = None
            target_group_name = None

            target_msg = self._get_context_message_for_target(reply_to)

            if str(chat_type).lower() == "group":
                if target_msg:
                    target_group_id = target_msg.extra.get("group_id")
                    target_group_name = target_msg.extra.get("group_name")
            else:
                if target_msg:
                    target_user_id = target_msg.sender_id
                    target_user_name = target_msg.sender_name
                if not target_user_id:
                    target_user_id = context.triggering_user_id

            extra: dict[str, str] = {}
            if target_user_id:
                extra["target_user_id"] = target_user_id
            if target_user_name:
                extra["target_user_name"] = target_user_name
            if target_group_id:
                extra["target_group_id"] = target_group_id
            if target_group_name:
                extra["target_group_name"] = target_group_name

            message = Message(
                message_id=f"action_{self.action_name}_{uuid4().hex}",
                content=content,
                processed_plain_text=content,
                message_type=MessageType.TEXT,
                sender_id=bot_info.get("bot_id", "") if bot_info else "",
                sender_name=bot_info.get("bot_name", "Bot") if bot_info else "Bot",
                platform=platform,
                chat_type=chat_type,
                stream_id=target_stream_id,
                reply_to=reply_to,
            )
            message.extra.update(extra)

            from src.core.transport.message_send import get_message_sender

            sender = get_message_sender()
            yield None
            await self._sleep_for_typing_delay(content)
            success = await sender.send_message(message)
            self._mark_sub_agent_bonus_on_success(success)
            yield success, f"已发送消息:{content}"
            return

        # 4) 非 reply_to 分支：处理 at（含 content 前缀解析出来的 hint）
        at_hint = (at or at_prefix_hint or "").strip().lstrip("@").strip()
        if not at_hint:
            yield None
            await self._sleep_for_typing_delay(content)
            success = await self._send_to_stream(content)
            self._mark_sub_agent_bonus_on_success(success)
            yield success, f"已发送消息:{content}"
            return

        # 5) 群聊场景下解析 at 目标
        target_stream_id = chat_stream.stream_id
        platform = chat_stream.platform
        chat_type = chat_stream.chat_type

        if str(chat_type).lower() != "group":
            # 非群聊忽略 at，按普通发送处理
            yield None
            await self._sleep_for_typing_delay(content)
            success = await self._send_to_stream(content)
            self._mark_sub_agent_bonus_on_success(success)
            yield success, f"已发送消息:{content}"
            return

        from src.core.managers.adapter_manager import get_adapter_manager
        from src.core.utils.user_query_helper import get_user_query_helper

        bot_info = await get_adapter_manager().get_bot_info_by_platform(platform)

        if at_hint.isdigit():
            at_user_id: str | None = at_hint
        else:
            at_user_id = await get_user_query_helper().resolve_user_id(platform, at_hint)

        if not at_user_id:
            logger.info(f"无法定位 at 目标: {at_hint}，降级为普通回复")
            yield None
            await self._sleep_for_typing_delay(content)
            success = await self._send_to_stream(content)
            self._mark_sub_agent_bonus_on_success(success)
            yield success, f"已发送消息:{content}"
            return

        target_group_id = None
        target_group_name = None
        last_msg = self._get_context_message_for_target()
        if last_msg:
            target_group_id = last_msg.extra.get("group_id")
            target_group_name = last_msg.extra.get("group_name")

        extra = {"at_user_id": str(at_user_id)}
        if target_group_id:
            extra["target_group_id"] = target_group_id
        if target_group_name:
            extra["target_group_name"] = target_group_name

        message = Message(
            message_id=f"action_{self.action_name}_{uuid4().hex}",
            content=content,
            processed_plain_text=content,
            message_type=MessageType.TEXT,
            sender_id=bot_info.get("bot_id", "") if bot_info else "",
            sender_name=bot_info.get("bot_name", "Bot") if bot_info else "Bot",
            platform=platform,
            chat_type=chat_type,
            stream_id=target_stream_id,
        )
        message.extra.update(extra)

        from src.core.transport.message_send import get_message_sender

        sender = get_message_sender()
        yield None
        await self._sleep_for_typing_delay(content)
        success = await sender.send_message(message)
        self._mark_sub_agent_bonus_on_success(success)
        yield success, f"已发送消息:{content}"
