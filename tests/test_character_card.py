"""角色卡三层结构测试。"""

from __future__ import annotations

from neo_fatum_chatter.domain.character_card import CharacterCard


class _CharConfig:
    def __init__(self, redlines=None, hidden_facts=None) -> None:
        self.redlines = redlines or []
        self.hidden_facts = hidden_facts or []


def test_from_config_parses_list_and_loose_string_formats() -> None:
    cfg = _CharConfig(
        redlines=["不会发语音"],
        hidden_facts=[
            {"fact": "其实早就知道她换了头像", "condition": "她提到换头像"},
            "其实不喜欢下雨天 | 她抱怨天气时",
        ],
    )
    card = CharacterCard.from_config(cfg)
    assert card.redlines == ["不会发语音"]
    assert len(card.hidden_facts) == 2
    assert card.hidden_facts[1].condition == "她抱怨天气时"


def test_hidden_facts_hidden_until_revealed() -> None:
    card = CharacterCard.from_config(
        _CharConfig(hidden_facts=[{"fact": "偷偷准备了礼物", "condition": "到生日那天"}])
    )
    text = card.render_prompt()
    assert "偷偷准备了礼物" not in text       # 未揭示：内容不进 prompt
    assert "还没说出口的事" in text            # 但提示模型"有秘密"

    newly = card.reveal([card.hidden_facts[0].id])
    assert len(newly) == 1
    text = card.render_prompt()
    assert "偷偷准备了礼物" in text            # 揭示后注入
    assert card.reveal([card.hidden_facts[0].id]) == []  # 幂等


def test_reveal_state_persists_via_export_merge() -> None:
    card = CharacterCard.from_config(
        _CharConfig(hidden_facts=[{"fact": "秘密A", "condition": "x"}])
    )
    card.reveal([card.hidden_facts[0].id])
    state = card.export_state()

    fresh_card = CharacterCard.from_config(
        _CharConfig(hidden_facts=[{"fact": "秘密A", "condition": "x"}])
    )
    fresh_card.merge_state(state)
    assert fresh_card.hidden_facts[0].revealed is True

    overlay_state = dict(state)
    overlay_state["active_overlay"] = {"name": "猫娘", "persona": "尾巴……"}
    fresh_card.merge_state(overlay_state)
    assert fresh_card.overlay_name == "猫娘"


def test_overlay_renders_with_leak_hint() -> None:
    card = CharacterCard()
    card.set_overlay("猫娘室友", "说话带喵，但依然是你")
    text = card.render_prompt()
    assert "猫娘室友" in text
    assert "漏出来" in text
    card.clear_overlay()
    assert "猫娘室友" not in card.render_prompt()


def test_empty_card_renders_nothing() -> None:
    assert CharacterCard().render_prompt() == ""
    assert CharacterCard().hidden_condition_brief() == ""


def test_redlines_merged_into_core_safety_blocks() -> None:
    """红线只读追加到宿主安全准则/禁止行为末尾，不写回 core.toml。"""
    from neo_fatum_chatter.prompts.modules import _merge_redlines_into

    safety = ["准则A"]
    negative = ["禁止B"]
    merged_safety, merged_negative = _merge_redlines_into(
        safety, negative, ["不会发语音"]
    )

    assert merged_safety == ["准则A", "不会发语音"]
    assert merged_negative == ["禁止B", "不会发语音"]
    # 只读合并：原列表不被就地修改
    assert safety == ["准则A"]
    assert negative == ["禁止B"]
    # 空红线时保持原样
    empty_safety, empty_negative = _merge_redlines_into(safety, negative, [])
    assert empty_safety == ["准则A"]
    assert empty_negative == ["禁止B"]
