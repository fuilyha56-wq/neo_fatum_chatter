"""新增模块的基本测试。

不依赖框架（src.kernel/src.app），仅测试纯逻辑。
通过 mock 框架模块绕过 rich/kernel 等依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

# ── 框架 mock：在 import 插件模块前拦截所有框架 import ──

_MOCK_MODULES = [
    "rich", "rich.console",
    "src", "src.kernel", "src.kernel.llm", "src.kernel.llm.exceptions",
    "src.kernel.logger", "src.kernel.logger.logger",
    "src.kernel.concurrency", "src.kernel.config",
    "src.kernel.scheduler", "src.kernel.scheduler.types",
    "src.kernel.storage", "src.kernel.db",
    "src.app", "src.app.plugin_system", "src.app.plugin_system.api",
    "src.app.plugin_system.api.log_api",
    "src.app.plugin_system.api.event_api",
    "src.app.plugin_system.api.llm_api",
    "src.app.plugin_system.api.stream_api",
    "src.app.plugin_system.api.prompt_api",
    "src.app.plugin_system.base",
    "src.app.plugin_system.types",
    "src.core", "src.core.prompt", "src.core.models",
    "src.core.models.stream", "src.core.models.message",
    "src.core.transport", "src.core.transport.distribution",
    "src.core.transport.distribution.stream_loop_manager",
    "src.core.managers", "src.core.managers.media_manager",
]

for mod_name in _MOCK_MODULES:
    if mod_name not in sys.modules:
        mock_mod = ModuleType(mod_name)
        # 让框架模块的属性访问都返回 MagicMock
        mock_mod.__dict__.setdefault("__all__", [])
        sys.modules[mod_name] = mock_mod

# 关键 mock 对象
_llm_mock = sys.modules["src.kernel.llm"]
_llm_mock.ROLE = MagicMock()  # type: ignore[attr-defined]
_llm_mock.ROLE.USER = "USER"  # type: ignore[attr-defined]
_llm_mock.ROLE.SYSTEM = "SYSTEM"  # type: ignore[attr-defined]
_llm_mock.ROLE.ASSISTANT = "ASSISTANT"  # type: ignore[attr-defined]
_llm_mock.ROLE.TOOL_RESULT = "TOOL_RESULT"  # type: ignore[attr-defined]
_llm_mock.LLMPayload = MagicMock  # type: ignore[attr-defined]
_llm_mock.Text = MagicMock  # type: ignore[attr-defined]
_llm_mock.Content = MagicMock  # type: ignore[attr-defined]

_log_mock = sys.modules["src.app.plugin_system.api.log_api"]
_log_mock.get_logger = lambda name: MagicMock()  # type: ignore[attr-defined]

_concurrency_mock = sys.modules["src.kernel.concurrency"]
_concurrency_mock.get_task_manager = MagicMock  # type: ignore[attr-defined]

_event_mock = sys.modules["src.app.plugin_system.api.event_api"]
_event_mock.EventDecision = MagicMock()  # type: ignore[attr-defined]
_event_mock.EventDecision.PASS = "PASS"  # type: ignore[attr-defined]
_event_mock.EventDecision.SUCCESS = "SUCCESS"  # type: ignore[attr-defined]

_base_mock = sys.modules["src.app.plugin_system.base"]
_base_mock.BaseEventHandler = object  # type: ignore[attr-defined]
_base_mock.BasePlugin = object  # type: ignore[attr-defined]
_base_mock.register_plugin = lambda cls: cls  # type: ignore[attr-defined]
_base_mock.Stop = MagicMock  # type: ignore[attr-defined]
_base_mock.Wait = MagicMock  # type: ignore[attr-defined]

# 插件根加入 path
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module_directly(name: str, filepath: str):
    """直接从文件加载模块，绕过 __init__.py 包初始化。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, filepath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 预加载纯逻辑模块（绕过包 __init__.py 的间接依赖）
_turn_trigger = _load_module_directly(
    "neo_fatum_chatter.domain.turn_trigger",
    str(_PLUGIN_DIR / "domain" / "turn_trigger.py"),
)
_unread_policy = _load_module_directly(
    "neo_fatum_chatter.runtime.unread_policy",
    str(_PLUGIN_DIR / "runtime" / "unread_policy.py"),
)


# ═══════════════════════════════════════════════════════════════
# 1. turn_trigger 测试
# ═══════════════════════════════════════════════════════════════


class TestTurnTrigger:
    def test_all_triggers_exist(self):
        TurnTrigger = _turn_trigger.TurnTrigger
        assert TurnTrigger.NEW_MESSAGES == "new_messages"
        assert TurnTrigger.FOLLOWUP_TOOL_RESULT == "followup_tool_result"
        assert TurnTrigger.TIMEOUT_EXPIRED == "timeout_expired"
        assert TurnTrigger.IDLE_WAIT == "idle_wait"
        assert len(TurnTrigger) == 4

    def test_classify_new_messages_highest_priority(self):
        TurnTrigger = _turn_trigger.TurnTrigger
        classify_turn_trigger = _turn_trigger.classify_turn_trigger

        session = MagicMock()
        session.is_waiting.return_value = True
        result = classify_turn_trigger(
            has_unread=True,
            has_pending_tool_results=True,
            session=session,
            is_timeout=True,
        )
        assert result == TurnTrigger.NEW_MESSAGES

    def test_classify_followup_over_timeout(self):
        TurnTrigger = _turn_trigger.TurnTrigger
        classify_turn_trigger = _turn_trigger.classify_turn_trigger

        session = MagicMock()
        session.is_waiting.return_value = True
        result = classify_turn_trigger(
            has_unread=False,
            has_pending_tool_results=True,
            session=session,
            is_timeout=True,
        )
        assert result == TurnTrigger.FOLLOWUP_TOOL_RESULT

    def test_classify_timeout(self):
        TurnTrigger = _turn_trigger.TurnTrigger
        classify_turn_trigger = _turn_trigger.classify_turn_trigger

        session = MagicMock()
        session.is_waiting.return_value = True
        result = classify_turn_trigger(
            has_unread=False,
            has_pending_tool_results=False,
            session=session,
            is_timeout=True,
        )
        assert result == TurnTrigger.TIMEOUT_EXPIRED

    def test_classify_idle_wait(self):
        TurnTrigger = _turn_trigger.TurnTrigger
        classify_turn_trigger = _turn_trigger.classify_turn_trigger

        session = MagicMock()
        session.is_waiting.return_value = False
        result = classify_turn_trigger(
            has_unread=False,
            has_pending_tool_results=False,
            session=session,
            is_timeout=False,
        )
        assert result == TurnTrigger.IDLE_WAIT


# ═══════════════════════════════════════════════════════════════
# 2. unread_policy 测试
# ═══════════════════════════════════════════════════════════════


class TestUnreadPolicy:
    def test_is_proactive_trigger_message(self):
        is_proactive_trigger_message = _unread_policy.is_proactive_trigger_message

        msg_proactive = MagicMock()
        msg_proactive.message_id = "proactive_abc123"
        assert is_proactive_trigger_message(msg_proactive) is True

        msg_real = MagicMock()
        msg_real.message_id = "user_msg_001"
        assert is_proactive_trigger_message(msg_real) is False

    def test_split_proactive_triggers(self):
        split_proactive_triggers = _unread_policy.split_proactive_triggers

        msgs = []
        for mid in ["user_1", "proactive_a", "user_2", "proactive_b"]:
            m = MagicMock()
            m.message_id = mid
            msgs.append(m)

        real, proactive = split_proactive_triggers(msgs)
        assert len(real) == 2
        assert len(proactive) == 2
        assert all(not m.message_id.startswith("proactive_") for m in real)

    def test_filter_interrupt_messages(self):
        filter_interrupt_messages = _unread_policy.filter_interrupt_messages

        known_ids = frozenset(["msg_1", "msg_2"])
        msgs = []
        for mid in ["msg_1", "msg_3", "proactive_x", "msg_4"]:
            m = MagicMock()
            m.message_id = mid
            msgs.append(m)

        result = filter_interrupt_messages(msgs, known_ids)
        # msg_1: known → filtered out
        # msg_3: new real → kept
        # proactive_x: proactive → filtered out
        # msg_4: new real → kept
        assert len(result) == 2
        result_ids = [m.message_id for m in result]
        assert "msg_3" in result_ids
        assert "msg_4" in result_ids


# ═══════════════════════════════════════════════════════════════
# 3. voice_call_history_handler 测试
# ═══════════════════════════════════════════════════════════════


class TestVoiceCallHistoryHandler:
    def test_summarize_empty_call(self):
        from handlers.voice_call_history_handler import VoiceCallHistoryHandler

        user_summary, assistant_summary, ts = VoiceCallHistoryHandler._summarize_messages([])
        assert "没有任何对话发生" in user_summary
        assert "已收到" in assistant_summary

    def test_summarize_basic_call(self):
        from handlers.voice_call_history_handler import VoiceCallHistoryHandler

        messages = [
            {"role": "system", "text": "语音通话已接通", "ts": 1000.0},
            {"role": "assistant", "text": "你好呀", "ts": 1001.0},
            {"role": "user", "text": "嗨，最近怎么样？", "ts": 1002.0},
            {"role": "assistant", "text": "还不错，在看书", "ts": 1003.0},
            {"role": "user", "text": "看什么书？", "ts": 1004.0},
            {"role": "assistant", "text": "三体", "ts": 1005.0},
            {"role": "system", "text": "语音通话已结束", "ts": 1006.0},
        ]

        user_summary, assistant_summary, first_ts = (
            VoiceCallHistoryHandler._summarize_messages(messages)
        )

        # 验证边界标记
        assert "语音通话已接通" in user_summary
        assert "语音通话已结束" in assistant_summary
        # 验证对话稿
        assert "【第 1 轮 / 用户】嗨，最近怎么样？" in user_summary
        assert "【第 2 轮 / 用户】看什么书？" in user_summary
        assert "【你的回应（接通时）】你好呀" in user_summary
        assert "【你的回应（第 1 轮回应）】还不错，在看书" in user_summary
        # 验证时间戳
        assert first_ts == 1002.0

    def test_summarize_no_user_messages(self):
        from handlers.voice_call_history_handler import VoiceCallHistoryHandler

        messages = [
            {"role": "system", "text": "通话开始", "ts": 100.0},
            {"role": "assistant", "text": "喂？", "ts": 101.0},
            {"role": "system", "text": "通话结束", "ts": 102.0},
        ]
        user_summary, assistant_summary, first_ts = (
            VoiceCallHistoryHandler._summarize_messages(messages)
        )
        assert "【你的回应（接通时）】喂？" in user_summary
        # 无 user 消息时用 fallback_ts
        assert first_ts == 100.0

    def test_summarize_invalid_messages_skipped(self):
        from handlers.voice_call_history_handler import VoiceCallHistoryHandler

        messages = [
            "not a dict",
            {"role": "user", "text": "", "ts": 1.0},  # empty text skipped
            {"role": "user", "text": "hello", "ts": 2.0},
        ]
        user_summary, _, first_ts = VoiceCallHistoryHandler._summarize_messages(messages)
        assert "hello" in user_summary
        assert first_ts == 2.0
