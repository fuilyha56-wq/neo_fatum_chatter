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

    # 世界状态（现实登记簿）：日程化日常 + 场景证据。
    # 设计移植自 private_companion：按五段日程窗口与作息锚点推导
    # "此刻在做什么"，角色自述/观察等事实级条目优先于作息推断。
    # story 登记簿激活时不渲染——剧情世界有自己的状态块。
    world_cfg = getattr(config, "world", None)
    if (world_cfg is None or world_cfg.enabled) and (
        getattr(session, "active_register", "reality") != "story"
    ):
        world_text = _build_world_state_text(session, world_cfg)
        if world_text:
            contributions.append(
                ContextContribution(
                    source="nfc.world_state",
                    owner="self_state",
                    scope="turn",
                    priority=65,
                    content=world_text,
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

    # 角色卡（覆层 / 隐藏事实）；红线已并入系统提示词安全节，不再单独渲染
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


def _build_world_state_text(session: NFCSession, world_cfg: Any) -> str:
    """组装现实登记簿世界状态块：日程化日常 + 已确认场景证据。"""
    from ...domain.daily_life import parse_clock

    daily_life = session.daily_life
    # 先滚动并收口过期条目，再同步配置默认值，避免临时位置失效后
    # 当前轮仍遗漏持久化默认位置。
    daily_life.ensure_day()

    # 配置同步（幂等）：锚点/位置/作息模板以配置为准
    if world_cfg is not None:
        wake = parse_clock(getattr(world_cfg, "wake_time", "") or "")
        sleep = parse_clock(getattr(world_cfg, "sleep_time", "") or "")
        if wake >= 0:
            daily_life.wake_minute = wake
        if sleep >= 0:
            daily_life.sleep_minute = sleep
        default_location = str(getattr(world_cfg, "location", "") or "").strip()
        if default_location and not daily_life.location:
            daily_life.location = default_location[:40]
        routine = getattr(world_cfg, "routine", None)
        if isinstance(routine, dict) and routine:
            from ...domain.daily_life import normalize_window

            daily_life.routine = {
                normalize_window(key): str(value or "").strip()
                for key, value in routine.items()
                if normalize_window(key) and str(value or "").strip()
            }

    world_text = daily_life.render_world_text()

    # 场景证据：S1 评估等来源确认的现实事实（防幻觉语义保留）
    scene = getattr(session, "scene_state", None)
    evidence_lines: list[str] = []
    for item in (scene.evidence if scene is not None else [])[-6:]:
        if not item.content.strip():
            continue
        confidence = max(0.0, min(1.0, float(item.confidence)))
        if confidence >= 0.9 and getattr(scene, "certainty", "unknown") == "confirmed":
            label = "已确认"
        elif confidence >= 0.6:
            label = "对话线索"
        else:
            label = "不确定线索"
        evidence_lines.append(f"- [{label}] {item.content}")

    sections: list[str] = []
    if world_text:
        sections.append(world_text)
    if evidence_lines:
        sections.append(
            "# 现实场景证据（按置信度分级）\n"
            + "\n".join(evidence_lines)
            + "\n- 以上内容来自对话证据；线索不等于确认事实，未提及的环境细节一律不要臆造。"
        )
    return "\n\n".join(sections)
