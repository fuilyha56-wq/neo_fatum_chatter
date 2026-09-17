"""主动思考提示词自定义与多工具文案回归测试。

覆盖：
1. ``proactive_prompt_override`` 的校验（占位符白名单 / XML 配对）与回退；
2. ``build_proactive_context`` 每次触发时重新解析模板（热更新立即生效）；
3. ``register_nfc_prompts`` 用覆盖式注册，热重载后模板文本真正更新；
4. 每轮 user_text 重申与主动决策指令不再把工具集限定为 nfc_reply/do_nothing。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import neo_fatum_chatter.prompts.modules as prompts_modules
from neo_fatum_chatter.context.planner import ContextPlanner
from neo_fatum_chatter.prompts.modules import (
    _resolve_proactive_prompt_template,
    _validate_proactive_prompt_override,
    build_proactive_context,
    register_nfc_prompts,
)
from neo_fatum_chatter.prompts.templates import (
    NFC_PROACTIVE_DECISION_TOOL_CALLING,
    NFC_PROACTIVE_PROMPT,
)

_VALID_CUSTOM = (
    "<my_proactive>\n"
    "现在是 {current_time}，沉默了 {silence_duration}。\n"
    "近期互动：{recent_activity}\n"
    "{proactive_decision_instruction}\n"
    "</my_proactive>"
)


def _fake_config(override: str) -> SimpleNamespace:
    return SimpleNamespace(
        prompt=SimpleNamespace(proactive_prompt_override=override),
    )


# ═══════════════════════════════════════════════════════════════
# 1. 校验器
# ═══════════════════════════════════════════════════════════════


class TestValidateProactiveOverride:
    def test_valid_custom_passes(self):
        ok, reason = _validate_proactive_prompt_override(_VALID_CUSTOM)
        assert ok, reason

    def test_unknown_placeholder_rejected(self):
        template = "现在是 {current_time}，你好 {nickname}"
        ok, reason = _validate_proactive_prompt_override(template)
        assert not ok
        assert "nickname" in reason

    def test_unpaired_xml_tag_rejected(self):
        template = "<spontaneous_thought>\n现在是 {current_time}\n"
        ok, reason = _validate_proactive_prompt_override(template)
        assert not ok
        assert "XML" in reason

    def test_empty_rejected(self):
        ok, _reason = _validate_proactive_prompt_override("   ")
        assert not ok


# ═══════════════════════════════════════════════════════════════
# 2. 解析器：配置读取 + 失败回退
# ═══════════════════════════════════════════════════════════════


class TestResolveProactiveTemplate:
    def test_no_config_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(prompts_modules, "get_config", lambda _name: None)
        assert _resolve_proactive_prompt_template() == NFC_PROACTIVE_PROMPT

    def test_valid_override_used(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            prompts_modules,
            "get_config",
            lambda _name: _fake_config(_VALID_CUSTOM),
        )
        assert _resolve_proactive_prompt_template() == _VALID_CUSTOM

    def test_invalid_override_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            prompts_modules,
            "get_config",
            lambda _name: _fake_config("坏模板 {nickname} <a>"),
        )
        assert _resolve_proactive_prompt_template() == NFC_PROACTIVE_PROMPT


# ═══════════════════════════════════════════════════════════════
# 3. build_proactive_context：渲染 + 热更新
# ═══════════════════════════════════════════════════════════════


class TestBuildProactiveContext:
    @pytest.mark.asyncio
    async def test_default_template_renders_tool_combination_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(prompts_modules, "get_config", lambda _name: None)
        result = await build_proactive_context(
            silence_minutes=90,
            recent_activity="用户: 在忙吗",
        )
        assert "<spontaneous_thought>" in result
        assert "1.5 小时" in result
        assert "用户: 在忙吗" in result
        # 主动决策指令必须告知可组合其他已注册工具（发图等）
        assert "组合调用对应的已注册工具" in result

    @pytest.mark.asyncio
    async def test_custom_override_rendered(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            prompts_modules,
            "get_config",
            lambda _name: _fake_config(_VALID_CUSTOM),
        )
        result = await build_proactive_context(
            silence_minutes=30,
            recent_activity="（无）",
            scheduled_reason="答应睡前道晚安",
        )
        assert "<my_proactive>" in result
        assert "30 分钟" in result
        # 决策指令占位被渲染为当前默认指令文本
        assert NFC_PROACTIVE_DECISION_TOOL_CALLING.strip() in result
        # 预约理由外层包装保留
        assert "答应睡前道晚安" in result

    @pytest.mark.asyncio
    async def test_override_hot_update_takes_effect_next_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        config_holder = {"value": ""}
        monkeypatch.setattr(
            prompts_modules,
            "get_config",
            lambda _name: _fake_config(config_holder["value"]),
        )
        first = await build_proactive_context(10, "")
        assert "<spontaneous_thought>" in first

        config_holder["value"] = _VALID_CUSTOM
        second = await build_proactive_context(10, "")
        assert "<my_proactive>" in second


# ═══════════════════════════════════════════════════════════════
# 4. register_nfc_prompts 覆盖式注册（热重载生效）
# ═══════════════════════════════════════════════════════════════


def _fake_core_config() -> SimpleNamespace:
    personality = SimpleNamespace(
        nickname="小狐",
        alias_names=[],
        personality_core="温柔",
        personality_side="",
        identity="",
        background_story="",
        reply_style="",
        safety_guidelines=[],
        negative_behaviors=[],
    )
    return SimpleNamespace(personality=personality)


class TestRegisterOverwrites:
    def test_re_register_updates_proactive_template(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from src.core.prompt.manager import PromptManager

        pm = PromptManager()
        monkeypatch.setattr(prompts_modules, "get_prompt_manager", lambda: pm)
        monkeypatch.setattr(prompts_modules, "get_core_config", _fake_core_config)
        monkeypatch.setattr(prompts_modules, "get_config", lambda _name: None)

        register_nfc_prompts()
        registered = pm.get_template("NFC_proactive_prompt")
        assert registered is not None
        assert registered.template == NFC_PROACTIVE_PROMPT

        # 热重载：换上自定义模板后再次注册，模板文本应被覆盖更新
        monkeypatch.setattr(
            prompts_modules,
            "get_config",
            lambda _name: _fake_config(_VALID_CUSTOM),
        )
        register_nfc_prompts()
        updated = pm.get_template("NFC_proactive_prompt")
        assert updated is not None
        assert updated.template == _VALID_CUSTOM


# ═══════════════════════════════════════════════════════════════
# 5. 每轮 user_text 重申不再限定工具集
# ═══════════════════════════════════════════════════════════════


class TestUserTurnReminder:
    @pytest.mark.asyncio
    async def test_reminder_allows_third_party_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        session = SimpleNamespace(pending_proactive_context="")
        config = SimpleNamespace(
            flashback=SimpleNamespace(injection_point="default_chatter_user_prompt"),
        )
        plan = await ContextPlanner().plan_user_turn(
            formatted_unreads="用户: 看看这张图",
            stream_id="stream-1",
            session=session,
            config=config,
        )
        assert "[新消息]" in plan.user_text
        # 不再把工具集限定为 nfc_reply/do_nothing
        assert "仅包含工具调用（nfc_reply 或 do_nothing）" not in plan.user_text
        # 明确告知可组合其他已注册工具且按顺序执行
        assert "组合调用其他已注册工具" in plan.user_text
        assert "依次执行" in plan.user_text

    @pytest.mark.asyncio
    async def test_proactive_placeholder_removed_from_user_text(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        session = SimpleNamespace(
            pending_proactive_context="主动思考富上下文内容",
        )
        config = SimpleNamespace(
            flashback=SimpleNamespace(injection_point="default_chatter_user_prompt"),
        )
        plan = await ContextPlanner().plan_user_turn(
            formatted_unreads="》2026-09-16 10:00:00》系统 [消息id:proactive_ab]： [proactive_trigger] 主动思考触发",
            stream_id="stream-1",
            session=session,
            config=config,
        )
        assert "[proactive_trigger]" not in plan.user_text
        # 会话读取后清空，避免下一轮重复注入
        assert session.pending_proactive_context == ""
        proactive_contributions = [
            c for c in plan.contributions
            if c.source == "nfc.proactive_trigger"
        ]
        assert len(proactive_contributions) == 1


# ═══════════════════════════════════════════════════════════════
# 6. 模板文案：感知跟进提示同样放开工具集
# ═══════════════════════════════════════════════════════════════


def test_perception_followup_mentions_other_tools():
    from neo_fatum_chatter.prompts.templates import (
        NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING,
    )

    assert "nfc_reply 或 do_nothing）执行你的决策" not in (
        NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING
    )
    assert "其他已注册工具" in NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING


def test_proactive_decision_instruction_mentions_other_tools():
    assert "同一套工具" in NFC_PROACTIVE_DECISION_TOOL_CALLING
    assert "已注册工具" in NFC_PROACTIVE_DECISION_TOOL_CALLING
