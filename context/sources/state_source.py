"""NFC 内部状态渲染 source。

把核心机制的各状态层（内驱 / 剧情世界 / 信念 / 角色卡 / 备忘录）渲染为
turn 级 ContextContribution，经 transient extra_payload 注入本轮请求，
发送后剥离——既让模型"带着状态说话"，又不污染持久链与前缀缓存。

所有渲染函数在状态中性时返回 None / 空列表，避免每轮输出
千篇一律的占位文本（那既浪费 token 又制造缓存噪音）。
"""

from __future__ import annotations

from typing import Any

from ...domain.session_state import NFCSession
from ..types import ContextContribution


def build_state_contributions(
    session: NFCSession,
    config: Any,
) -> list[ContextContribution]:
    """收集全部内部状态贡献。"""
    contributions: list[ContextContribution] = []

    # 内驱状态（潜意识）——中性时不渲染
    if getattr(config, "drives", None) and config.drives.enabled:
        drives_text = session.drives.render_state_text()
        if drives_text:
            contributions.append(
                ContextContribution(
                    source="nfc.drives",
                    owner="self_state",
                    scope="turn",
                    priority=60,
                    content=drives_text,
                )
            )

    # 剧情世界（story 登记簿激活时）
    story_world = getattr(session, "story_world", None)
    if (
        getattr(session, "active_register", "reality") == "story"
        and story_world is not None
    ):
        story_text = story_world.render_story_text()
        if story_text:
            contributions.append(
                ContextContribution(
                    source="nfc.story_world",
                    owner="scene_evidence",
                    scope="turn",
                    priority=75,
                    content=story_text,
                )
            )

    # 信念层（当前登记簿为主，跨登记簿只留钩子）
    beliefs_cfg = getattr(config, "beliefs", None)
    if beliefs_cfg is None or beliefs_cfg.enabled:
        active_register = getattr(session, "active_register", "reality")
        beliefs_text = session.beliefs.for_prompt(register=active_register)
        if beliefs_text:
            contributions.append(
                ContextContribution(
                    source="nfc.beliefs",
                    owner=(
                        "user_state"
                        if active_register == "reality"
                        else "relationship_state"
                    ),
                    scope="turn",
                    priority=45,
                    content=beliefs_text,
                )
            )

    # 角色卡（覆层 / 隐藏事实 / 红线）
    char_cfg = getattr(config, "character", None)
    if char_cfg is not None:
        from ...domain.character_card import CharacterCard

        card = CharacterCard.from_config(char_cfg)
        card.merge_state(getattr(session, "character_state", None))
        card_text = card.render_prompt()
        if card_text:
            contributions.append(
                ContextContribution(
                    source="nfc.character_card",
                    owner="policy",
                    scope="turn",
                    priority=70,
                    content=card_text,
                )
            )

    # 备忘录（LLM 显式便签）——无有效条目时不渲染
    memo_cfg = getattr(config, "memo", None)
    if memo_cfg is None or memo_cfg.enabled:
        memo_text = session.memos.render()
        if memo_text:
            contributions.append(
                ContextContribution(
                    source="nfc.memo",
                    owner="notice",
                    scope="turn",
                    priority=80,
                    content=memo_text,
                )
            )

    return contributions
