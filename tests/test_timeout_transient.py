"""超时提示 transient 化与 KFC 借鉴项的接线测试。"""

from __future__ import annotations

import asyncio
import time

from src.kernel.llm import LLMPayload, ROLE, Text

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.models import WaitingConfig
from neo_fatum_chatter.runtime.turn_controller import prepare_turn_input
from neo_fatum_chatter.services.timeout_service import TimeoutService


class _FakeChatter:
    """覆盖 TIMEOUT_EXPIRED 分支所需的最小 chatter 表面。"""

    def __init__(self, unreads: list) -> None:
        self._unreads = unreads
        self.stream_id = "stream-1"

    async def fetch_unreads(self, time_format: str = ""):
        return "", list(self._unreads)

    @staticmethod
    def format_message_line(msg, time_format: str = "") -> str:
        return f"{getattr(msg, 'processed_plain_text', '')}"

    @staticmethod
    def _extract_timestamp(msg) -> float:
        return float(getattr(msg, "time", 0) or 0)


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads = [LLMPayload(ROLE.USER, Text("[消息id:m1] 你在吗"))]

    def add_payload(self, payload, position=None) -> None:
        if position is None:
            self.payloads.append(payload)
        else:
            self.payloads.insert(position, payload)


class _FakeChatStream:
    platform = "qq"


def test_timeout_prompt_is_transient_not_persisted_to_chain() -> None:
    """超时提示应以 extra_payload 返回（request-only），不进 response 链。"""
    asyncio.run(_run_timeout_turn())


async def _run_timeout_turn() -> None:
    config = NFCConfig()
    session = NFCSession(user_id="u", stream_id="stream-1")
    session.set_waiting(
        WaitingConfig(
            expected_reaction="会回我",
            max_wait_seconds=30,
            started_at=time.time() - 120,  # 已超时
        )
    )
    chatter = _FakeChatter(unreads=[])
    response = _FakeResponse()
    chain_before = list(response.payloads)

    result = await prepare_turn_input(
        chatter,
        response,
        _FakeChatStream(),
        config,
        session,
        None,  # prompt_builder：超时分支不使用
        TimeoutService(config),
        None,  # image_budget
        has_history=False,
        history_images_injected=False,
        has_pending_tool_results=False,
    )

    # 超时提示作为 transient extra_payload 返回
    assert result.extra_payload is not None
    timeout_text = _payload_text(result.extra_payload)
    assert "你发出消息已经过去" in timeout_text
    assert result.is_timeout_turn is True
    # response 链保持原样：提示文本没有写入链（会被 request_snapshot 带走）
    assert response.payloads == chain_before
    assert all("你发出消息已经过去" not in _payload_text(p) for p in response.payloads)
    # 内驱钩子生效：超时压低情绪
    assert session.drives.mood < 0.5


def test_drives_modulate_wait_in_commit_path() -> None:
    """内驱对等待时长的调制保持区间约束（直接验证调制函数与配置联动）。"""
    config = NFCConfig()
    assert config.drives.enabled is True
    session = NFCSession(user_id="u", stream_id="s")
    session.drives.neglect = 0.9
    session.drives.energy = 0.1

    modulated = session.drives.modulate_wait(
        config.wait.apply_rules(120.0, 0),
        strength=config.drives.modulation_strength,
    )
    assert modulated >= config.wait.min_seconds
    assert modulated <= config.wait.max_seconds
    assert modulated > 120.0  # 被冷落+疲惫 → 等更久


def _payload_text(payload: LLMPayload) -> str:
    content = payload.content
    if isinstance(content, Text):
        return content.text
    if isinstance(content, list):
        return "".join(item.text for item in content if isinstance(item, Text))
    return ""


def test_kfc_borrowed_config_fields_exist_with_defaults() -> None:
    config = NFCConfig()
    # 打断冷却递增 + 连续上限（KFC 同款）
    assert config.buffer.interrupt_cooldown == 3.0
    assert config.buffer.max_consecutive_interrupts == 3
    # 语义化延迟默认开启，打字速度配置复活
    assert config.reply.semantic_delays is True
    assert config.reply.typing_chars_per_sec == 15.0
    # 新核心 sections
    assert config.drives.enabled is True
    assert config.appraisal.enabled is True
    assert config.appraisal.model_task == "sub_actor"
    assert config.beliefs.enabled is True
    assert config.intent.enabled is True
    assert config.intent.fire_threshold == 0.75
