"""2.6.5 审查修复的回归测试。

锁定审查与线上日志发现的修复，防止回退：
1. Intent.grow 冲动值改为增量结算（原实现对同一段时间重复累加）；
2. exit_story 正确摘除 character_state 中的剧情覆层（原实现读错字段）；
3. beliefs.for_prompt 跨登记簿前缀按方向区分（原实现恒为"你们的故事里"）；
4. BeliefBook.decay 改为增量结算（原实现每次调用对全部历史天数重复扣减）；
5. 空回复打回重试不再被 reply_execution_failed 拦截（原实现下空包弹
   先被拒发置位、恰好错过重试窗口，日志表现为"回复完全发送失败"）；
6. S1 评估的 min_input_chars 按消息本体计量（原实现量的是带时间戳/
   昵称包装的格式化文本，导致"摸摸"也会多烧一次 sub_actor 请求）。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from neo_fatum_chatter.domain.beliefs import BeliefBook
from neo_fatum_chatter.domain.character_card import CharacterCard
from neo_fatum_chatter.domain.decision import Decision
from neo_fatum_chatter.domain.intent import KIND_TOPIC_HOOK, Intent, IntentQueue
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.domain.world import WorldTracker
from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.context.sources.state_source import build_state_contributions
from neo_fatum_chatter.domain.memo import (
    MEMO_MAX_ENTRIES,
    Memo,
    MemoBook,
    clamp_expire_hours,
)
from neo_fatum_chatter.models import NFCEventType

DAY = 86400.0
HOUR = 3600.0


# ── Bug 1: Intent.grow 增量结算 ───────────────────────────


def test_grow_is_idempotent_against_repeated_calls() -> None:
    base = time.time()
    intent = Intent(kind=KIND_TOPIC_HOOK, content="问问考试", urge=0.5, created_at=base)
    queue = IntentQueue([intent])

    queue.grow_all(base + 5 * HOUR)
    first = intent.urge
    queue.grow_all(base + 5 * HOUR)  # 同一时刻重复调用不应继续增长
    assert intent.urge == first

    queue.grow_all(base + 6 * HOUR)  # 再过 1 小时只多增长 1 小时的量
    assert abs(intent.urge - (0.5 + 0.06 * 6)) < 1e-6


def test_grow_settlement_survives_serialization() -> None:
    base = time.time()
    intent = Intent(kind=KIND_TOPIC_HOOK, content="周末问她", urge=0.5, created_at=base)
    intent.grow(base + 2 * HOUR)

    restored = Intent.from_dict(intent.to_dict())
    assert restored.last_grown_at == intent.last_grown_at

    restored.grow(base + 3 * HOUR)
    assert abs(restored.urge - (0.5 + 0.06 * 3)) < 1e-6


# ── Bug 2: exit_story 摘覆层 ──────────────────────────────


def test_exit_story_clears_overlay_from_character_state() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.character_state = {
        "revealed_fact_ids": ["hf0"],
        "active_overlay": {"name": "深夜店员", "persona": "便利店的夜班店员"},
    }
    WorldTracker.ensure_initialized(session)
    WorldTracker.apply_frame_event(session, "enter_story")
    session.story_world.story.update({"ongoing_event": "一起躲雨"})

    WorldTracker.apply_frame_event(session, "exit_story")

    assert session.character_state["active_overlay"] is None
    assert session.character_state["revealed_fact_ids"] == ["hf0"]  # 揭示状态保留
    assert session.story_archive[0].overlay_name == "深夜店员"

    # 渲染层不再出现覆层块
    card = CharacterCard.from_config(None)
    card.merge_state(session.character_state)
    assert "覆层" not in card.render_prompt()


# ── Bug 3: 跨登记簿前缀 ───────────────────────────────────


def test_for_prompt_cross_prefix_follows_direction() -> None:
    book = BeliefBook()
    book.upsert("她养了一只猫", register="reality", confidence=0.8)
    book.upsert("故事里她在找失踪的弟弟", register="story", confidence=0.8)

    # 现实视角：跨登记簿是故事
    reality_text = book.for_prompt(register="reality")
    assert "你们的故事里" in reality_text

    # 故事视角：跨登记簿是现实，前缀不应再说"你们的故事里"
    story_text = book.for_prompt(register="story")
    assert "现实中：她养了一只猫" in story_text
    assert "你们的故事里：现实中" not in story_text


# ── Bug 4: decay 增量结算 ─────────────────────────────────


def test_decay_is_incremental_not_repeated() -> None:
    base = time.time()
    book = BeliefBook()
    book.upsert("她最近在备考", confidence=0.5)
    belief = book.beliefs[0]
    belief.last_evidence_at = base - 3 * DAY

    book.decay(base)
    after_first = belief.confidence
    assert abs(after_first - (0.5 - 0.05 * 3)) < 1e-6

    book.decay(base + 60)  # 一分钟后再结算：几乎无新增衰减
    assert abs(belief.confidence - after_first) < 0.01

    book.decay(base + 2 * DAY)  # 两天后再结算：只加两天的量
    assert abs(belief.confidence - (after_first - 0.05 * 2)) < 1e-6


def test_reinforce_resets_decay_base() -> None:
    now = time.time()
    book = BeliefBook()
    book.upsert("她怕黑", confidence=0.5)
    belief = book.beliefs[0]
    belief.last_evidence_at = now - 3 * DAY

    book.decay(now)
    assert abs(belief.confidence - 0.35) < 1e-6

    book.upsert("她怕黑", confidence=0.5)  # 同文强化
    assert belief.evidence_count == 2
    assert belief.last_evidence_at >= now - 1  # 证据时间刷新
    assert abs(belief.confidence - 0.5975) < 1e-6  # 0.5 + 0.15×0.65

    book.decay(now + DAY)  # 一天后从新证据时间起算，只扣 1 天
    assert abs(belief.confidence - 0.5475) < 1e-6


# ── Bug 5: 空回复重试不被 execution_failed 拦截 ────────────


@pytest.mark.asyncio
async def test_orchestrator_retries_after_execution_failed_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空包弹被拒发（reply_execution_failed=True）时仍应打回重试。"""
    import neo_fatum_chatter.runtime.orchestrator as orchestrator_module
    from neo_fatum_chatter.runtime.orchestrator import execute_orchestrator
    from neo_fatum_chatter.runtime.turn_controller import (
        TurnControlResult,
        TurnInputResult,
    )
    from src.app.plugin_system.base import Stop
    from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult

    def make_response(call_id: str, content: str) -> SimpleNamespace:
        call = ToolCall(id=call_id, name="nfc_reply", args={"content": content})
        response = SimpleNamespace(
            payloads=[
                LLMPayload(ROLE.USER, Text("摸摸")),
                LLMPayload(ROLE.ASSISTANT, call),
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(
                        value="已发送" if content else "内容为空，未发送",
                        call_id=call_id,
                        name="nfc_reply",
                    ),
                ),
            ],
            call_list=[call],
            message="",
        )

        def add_payload(payload: LLMPayload) -> None:
            response.payloads.append(payload)

        response.add_payload = add_payload
        return response

    responses = [
        make_response("empty-1", ""),
        make_response("valid-2", "这就有内容了"),
    ]
    decisions = [
        Decision(
            actions=[{"type": "nfc_reply", "content": []}],
            has_reply_action=True,
            has_meaningful_action=True,
            reply_execution_failed=True,  # 拒发已置位——原实现会因此跳过重试
        ),
        Decision(
            actions=[{"type": "nfc_reply", "content": ["这就有内容了"]}],
            visible_reply_segments=["这就有内容了"],
            has_reply_action=True,
            has_meaningful_action=True,
        ),
    ]
    send_count = 0
    committed: list[Decision] = []

    async def fake_send(response, max_retries):
        nonlocal send_count
        result = responses[send_count]
        send_count += 1
        return result

    async def fake_parse(*args, **kwargs):
        return decisions.pop(0)

    async def fake_commit(*args, **kwargs):
        committed.append(args[1])
        return TurnControlResult(next_signal=Stop(0), return_after_yield=True)

    async def resolve(value):
        return value

    initial_response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("摸摸"))],
        call_list=[],
        message="",
    )
    session = SimpleNamespace(update_chain=lambda *a, **k: None)
    config = SimpleNamespace(
        general=SimpleNamespace(
            enabled=True,
            native_multimodal=False,
            models=[],
            model_task="actor",
            temperature=0.7,
            max_tokens=1024,
            max_compat_retries=0,
            max_empty_reply_retries=2,
            max_consecutive_llm_failures=3,
        ),
        buffer=SimpleNamespace(interrupt_enabled=False),
        debug=SimpleNamespace(show_prompt=False),
        prompt=SimpleNamespace(max_context_payloads=20, summary_enabled=False),
    )
    chatter = SimpleNamespace(
        stream_id="stream-test",
        _get_config=lambda: config,
        _get_session=lambda: resolve(session),
        _build_initial_context=lambda *args: resolve(
            (initial_response, None, SimpleNamespace(), SimpleNamespace(), False)
        ),
        _send_with_perceive_loop=fake_send,
        _get_virtual_trigger_message=lambda: resolve(SimpleNamespace()),
        fetch_unreads=lambda **kwargs: resolve(("", [])),
        flush_unreads=lambda messages: resolve(None),
        _save_session=lambda session: resolve(None),
        run_tool_call=lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(
        "src.app.plugin_system.api.stream_api.activate_stream",
        lambda stream_id: resolve(SimpleNamespace(stream_id=stream_id)),
    )
    monkeypatch.setattr(orchestrator_module, "get_model_set_by_task", lambda task: object())
    monkeypatch.setattr(
        orchestrator_module,
        "prepare_turn_input",
        lambda *args, **kwargs: resolve(
            TurnInputResult(response=args[1], unread_msgs=[])
        ),
    )
    monkeypatch.setattr(orchestrator_module, "parse_response_decision", fake_parse)
    monkeypatch.setattr(orchestrator_module, "commit_turn_decision", fake_commit)

    yielded = [signal async for signal in execute_orchestrator(chatter)]

    assert send_count == 2  # 空包弹被拒发后应发起重试请求
    assert committed[0].visible_reply_segments == ["这就有内容了"]
    assert len(yielded) == 1 and isinstance(yielded[0], Stop)


# ── Bug 6: S1 长度门按消息本体计量 ─────────────────────────


@pytest.mark.asyncio
async def test_s1_gate_uses_raw_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw_input_chars 低于阈值时不发起评估请求（哪怕格式化文本很长）。"""
    from neo_fatum_chatter.services import appraisal as appraisal_mod

    config = NFCConfig()
    session = NFCSession(user_id="u", stream_id="s")
    formatted = "》2026-09-12 18:30:52》[QQ:123] 某人 [消息id:456]： 摸摸"  # 30+ 字符

    create_calls = {"count": 0}

    def fail_create(*args, **kwargs):
        create_calls["count"] += 1
        raise AssertionError("长度门应拦截，不应发起 LLM 请求")

    monkeypatch.setattr(appraisal_mod, "get_model_set_by_task", lambda task: object())
    monkeypatch.setattr(appraisal_mod, "create_llm_request", fail_create)

    result = await appraisal_mod.appraise_messages(
        formatted, session=session, config=config, raw_input_chars=2
    )
    assert result is None
    assert create_calls["count"] == 0


@pytest.mark.asyncio
async def test_s1_appraises_when_raw_length_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw_input_chars 达标时正常评估（mock LLM 返回合法 JSON）。"""
    from neo_fatum_chatter.services import appraisal as appraisal_mod

    config = NFCConfig()
    session = NFCSession(user_id="u", stream_id="s")
    formatted = "》2026-09-12 18:30:52》[QQ:123] 某人 [消息id:456]： 今天好累啊，想聊聊"

    class FakeRequest:
        def add_payload(self, payload):  # noqa: ANN001
            pass

        def send(self):  # noqa: ANN001
            # 生产链 send() 返回可等待对象，await 后仍是可等待对象，
            # 再 await 才得到文本
            async def _outer():
                async def _inner():
                    return json.dumps(
                        {"emotion": "心疼", "urgency": 0.5, "defer_seconds": 3}
                    )

                return _inner()

            return _outer()

    monkeypatch.setattr(appraisal_mod, "get_model_set_by_task", lambda task: object())
    monkeypatch.setattr(appraisal_mod, "create_llm_request", lambda *a, **k: FakeRequest())

    result = await appraisal_mod.appraise_messages(
        formatted, session=session, config=config, raw_input_chars=11
    )
    assert result is not None
    assert result.emotion == "心疼"


# ── S1 与主模型并行：非阻塞收割（副决策语义） ──────────────


@pytest.mark.asyncio
async def test_harvest_applies_completed_task() -> None:
    """收割已完成的 S1 任务：状态落盘、defer 意见落到 respond_not_before。"""
    import asyncio

    from neo_fatum_chatter.services.appraisal import (
        AppraisalResult,
        harvest_pending_appraisal,
    )

    config = NFCConfig()
    session = NFCSession(user_id="u", stream_id="s")

    async def _done():
        return AppraisalResult(
            emotion="开心",
            topic_hooks=["她提到的演唱会"],
            defer_seconds=3.0,
        )

    session._s1_task = asyncio.create_task(_done())
    await asyncio.sleep(0.01)  # 让任务完成（done 后 result 仍可读取）
    harvest_pending_appraisal(session, config)

    assert session._s1_task is None
    assert "她提到的演唱会" in [i.content for i in session.intents.intents]
    assert session.respond_not_before > time.time()  # defer 意见已就位


def test_harvest_noop_without_task() -> None:
    from neo_fatum_chatter.services.appraisal import harvest_pending_appraisal

    session = NFCSession(user_id="u", stream_id="s")
    harvest_pending_appraisal(session, NFCConfig())  # 不应抛错
    assert getattr(session, "_s1_task", None) is None


@pytest.mark.asyncio
async def test_harvest_keeps_running_task_for_next_round() -> None:
    """未完成任务且不强制取消：留在槽位，由下一轮收割。"""
    import asyncio

    from neo_fatum_chatter.services.appraisal import harvest_pending_appraisal

    session = NFCSession(user_id="u", stream_id="s")

    async def _slow():
        await asyncio.sleep(5)

    task = asyncio.create_task(_slow())
    session._s1_task = task
    harvest_pending_appraisal(session, NFCConfig())  # 默认不取消

    assert session._s1_task is task  # 保留给下一轮
    task.cancel()


@pytest.mark.asyncio
async def test_harvest_cancels_stale_running_task() -> None:
    """批次过期（prepare_turn_input 收割）：未完成任务直接取消。"""
    import asyncio

    from neo_fatum_chatter.services.appraisal import harvest_pending_appraisal

    session = NFCSession(user_id="u", stream_id="s")

    async def _slow():
        await asyncio.sleep(5)

    task = asyncio.create_task(_slow())
    session._s1_task = task
    harvest_pending_appraisal(session, NFCConfig(), cancel_if_running=True)

    assert session._s1_task is None
    with pytest.raises(asyncio.CancelledError):
        await task  # 取消已请求：任务以 CancelledError 收场
