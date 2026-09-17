"""NFC 与 on_prompt_build 注入器的兼容回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from neo_fatum_chatter.context.renderer import ContextRenderer
from neo_fatum_chatter.context.sources.plugin_source import (
    collect_plugin_turn_contributions,
)
from src.core.prompt.template import PromptTemplate
from src.kernel.event import EventBus, EventDecision


@pytest.mark.asyncio
async def test_legacy_prompt_name_and_values_extra_are_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧注入器读取 prompt_name 并写 values.extra 时仍应生效。"""
    bus = EventBus(name="nfc-prompt-test")

    async def legacy_handler(event_name: str, params: dict):
        assert event_name == "on_prompt_build"
        assert params["name"] == "default_chatter_user_prompt"
        assert params["prompt_name"] == params["name"]
        values = dict(params["values"])
        values["extra"] = "每轮按场景判断是否调用图片工具"
        params["values"] = values
        return EventDecision.SUCCESS, params

    bus.subscribe("on_prompt_build", legacy_handler)
    monkeypatch.setattr("src.kernel.event.get_event_bus", lambda: bus)

    contributions = await collect_plugin_turn_contributions(
        prompt_name="default_chatter_user_prompt",
        content="[新消息]\n你好",
        stream_id="stream-1",
    )

    assert len(contributions) == 1
    assert contributions[0].source == "legacy.on_prompt_build.extra"
    assert "图片工具" in contributions[0].content


@pytest.mark.asyncio
async def test_structured_contribution_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新注入器追加结构化 contribution 时应原样进入规划层。"""
    bus = EventBus(name="nfc-structured-prompt-test")

    async def structured_handler(event_name: str, params: dict):
        params["context_contributions"].append(
            {
                "source": "test.injector",
                "owner": "policy",
                "scope": "turn",
                "priority": 50,
                "content": "需要时调用图片工具",
            }
        )
        return EventDecision.SUCCESS, params

    bus.subscribe("on_prompt_build", structured_handler)
    monkeypatch.setattr("src.kernel.event.get_event_bus", lambda: bus)

    contributions = await collect_plugin_turn_contributions(
        prompt_name="default_chatter_user_prompt",
        content="[新消息]\n你好",
        stream_id="stream-2",
    )

    assert [(item.source, item.owner, item.content) for item in contributions] == [
        ("test.injector", "policy", "需要时调用图片工具")
    ]


@pytest.mark.asyncio
async def test_system_prompt_uses_standard_prompt_build_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFC system prompt 必须经过标准 PromptTemplate.build 事件。"""
    bus = EventBus(name="nfc-system-prompt-test")

    async def injector(event_name: str, params: dict):
        assert params["name"] == "NFC_system_prompt"
        params["template"] = params["template"] + "\n已注入图片规则"
        return EventDecision.SUCCESS, params

    bus.subscribe("on_prompt_build", injector)
    monkeypatch.setattr("src.kernel.event.get_event_bus", lambda: bus)

    template = PromptTemplate(
        name="NFC_system_prompt",
        template="基础系统提示 {platform}",
    )
    manager = SimpleNamespace(get_template=lambda name: template if name == "NFC_system_prompt" else None)
    monkeypatch.setattr(
        "neo_fatum_chatter.context.renderer.get_prompt_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "neo_fatum_chatter.prompts.modules.build_mental_log_hint",
        lambda: "",
    )

    stream = SimpleNamespace(
        platform="qq",
        chat_type="private",
        bot_id="bot-1",
        stream_id="stream-3",
    )
    rendered = await ContextRenderer().build_system_prompt(stream)

    assert "基础系统提示 qq" in rendered
    assert "已注入图片规则" in rendered
