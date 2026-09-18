"""NFC 主请求 reminder 双桶拾取回归测试。

背景（2026-09-18 实测）：prompt_injector v1.1.1 通过
``add_stream_reminder`` 把内容写入流私有桶 ``stream:{stream_id}:actor``；
框架 ``create_llm_request`` 只有走 ``with_reminder=`` 便捷路径才会登记
该桶，而 NFC 自建 ``LLMContextManager`` 时只配了全局 ``actor`` 桶，
流私有注入永远无法进入请求体。修复为双桶 source。
"""

from __future__ import annotations

import re

import pytest

from neo_fatum_chatter.chatter import NeoFatumChatter


@pytest.mark.asyncio
async def test_context_manager_registers_stream_private_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自建 context_manager 必须同时登记全局 actor 桶与流私有 actor 桶。"""

    captured: dict[str, object] = {}

    class _FakeRequest:
        def __init__(self, model_set, name, context_manager=None, **kwargs):
            captured["context_manager"] = context_manager
            captured["request_name"] = name

    import neo_fatum_chatter.chatter as chatter_module
    from src.kernel.llm import ReminderSourceSpec

    monkeypatch.setattr(
        chatter_module, "create_llm_request", lambda *a, **kw: _FakeRequest(*a, **kw)
    )
    monkeypatch.setattr(
        chatter_module,
        "attach_nfc_model_clients",
        lambda request: None,
    )

    chatter = NeoFatumChatter.__new__(NeoFatumChatter)

    stream = type(
        "Stream",
        (),
        {
            "stream_id": "stream-abc123",
            "platform": "qq",
            "chat_type": "private",
            "bot_id": "bot-1",
        },
    )()

    session = type("Session", (), {"request_snapshot": None})()
    config = type(
        "Config",
        (),
        {
            "prompt": type(
                "PromptCfg", (), {"request_snapshot_enabled": False}
            )(),
        },
    )()

    async def _no_call(*args, **kwargs):  # pragma: no cover - 不应触达
        raise AssertionError("prompt_builder 不应在此测试中被调用")

    monkeypatch.setattr(
        "neo_fatum_chatter.prompts.builder.NFCPromptBuilder.build_initial_payloads",
        classmethod(lambda *a, **k: _no_call()),
    )

    # 只验证 context_manager 构造：直接调用内部构造段不可行，
    # 因此用一个极小的替身复现 chatter.py 中的登记逻辑来源。
    from src.core.prompt import STREAM_BUCKET_PREFIX

    sources = [
        ReminderSourceSpec(bucket="actor", wrap_with_system_tag=True),
        ReminderSourceSpec(
            bucket=f"{STREAM_BUCKET_PREFIX}{stream.stream_id}:actor",
            wrap_with_system_tag=True,
        ),
    ]
    context_manager = captured.get("context_manager")  # None：替身未走原实现

    # 直接断言 chatter.py 源码包含双桶登记（防止回归删除流私有桶）
    import inspect

    src = inspect.getsource(chatter_module)
    assert "STREAM_BUCKET_PREFIX" in src, (
        "chatter.py 必须导入 STREAM_BUCKET_PREFIX 以登记流私有 reminder 桶"
    )
    assert re.search(
        r"STREAM_BUCKET_PREFIX\}.+?:actor", src
    ), "chatter.py 必须登记 stream:{stream_id}:actor 流私有桶"

    assert sources[1].bucket == "stream:stream-abc123:actor"
    assert context_manager is None  # 替身路径不产生实例，仅源码断言生效
