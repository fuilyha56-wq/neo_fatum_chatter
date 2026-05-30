# 预约窗口主动发起 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持模型预约一个绝对时间窗口，在窗口内由后台 sub-actor 周期性判断是否唤醒主 actor，并在主 actor 不发时降低后续唤醒概率。

**Architecture:** 扩展现有 `schedule_proactive` 链路，不新增独立调度系统。模型写入一个预约窗口到 `NFCSession`，现有 `NFC_proactive_check` 周期任务扫描窗口；窗口内按 5~15 分钟节流并通过后台 sub-actor/轻量判定服务判断是否发布 `NFC.proactive_trigger`。主 actor 仍使用现有 `nfc_reply`、`do_nothing/pass_and_wait`、`schedule_proactive` 决策；若被窗口唤醒后没有回复，则保留窗口并衰减 interest。

**Tech Stack:** Python 3.11+、dataclasses、pytest、现有 `src.kernel.scheduler`、现有 EventBus、现有 NFC session JSON 持久化。

---

## 文件结构

- Modify: `actions/schedule_proactive.py`
  - 扩展 action 参数，支持 `start_at` / `end_at` / `context` / `interest`，同时保留 `delay_minutes` 兼容。
- Modify: `domain/decision.py`
  - 将 `ProactiveSchedule` 从单点延迟扩展为兼容单点/窗口的决策对象。
- Modify: `protocol/decision_parser.py`
  - 从模型工具调用中解析绝对时间窗口字段。
- Modify: `domain/session_state.py`
  - 增加窗口预约状态、序列化、反序列化和状态操作方法。
- Modify: `services/proactive_service.py`
  - 将决策对象应用到 session，负责解析绝对时间、兼容旧 `delay_minutes`。
- Modify: `thinker/proactive.py`
  - 扫描窗口，后台节流，执行 sub-actor 候选判断，发布触发候选。
- Modify: `plugin.py`
  - 在主动触发后根据主 actor 结果判断是否消费窗口或降低 interest。
- Modify: `runtime/orchestrator.py`
  - 在一次主 actor 决策结束后通知 `ProactiveService` 处理窗口唤醒结果。
- Modify: `handlers/proactive_handler.py`
  - 把窗口上下文传给主动发起 prompt。
- Modify: `prompts/modules.py`
  - 增强主动发起上下文，包含预约窗口和预约上下文。
- Modify: `config.py`
  - 增加窗口检查间隔、interest 衰减、最小 interest、最大窗口触发尝试等配置。
- Create: `tests/test_proactive_window.py`
  - 测试窗口解析、session 序列化、窗口检查、interest 衰减。

---

### Task 1: 扩展 session 窗口预约状态

**Files:**
- Modify: `domain/session_state.py`
- Test: `tests/test_proactive_window.py`

- [ ] **Step 1: Write failing tests for session window state**

Create `tests/test_proactive_window.py` with:

```python
"""主动预约窗口测试。"""

from __future__ import annotations

from neo_fatum_chatter.domain.session_state import NFCSession


def test_session_sets_and_clears_proactive_window():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )

    assert session.scheduled_proactive_start_at == 1_000.0
    assert session.scheduled_proactive_end_at == 2_000.0
    assert session.scheduled_proactive_context == "晚上问问状态"
    assert session.scheduled_proactive_interest == 0.8
    assert session.scheduled_proactive_check_interval == 600.0
    assert session.scheduled_proactive_check_count == 0

    session.clear_scheduled_proactive()

    assert session.scheduled_proactive_at is None
    assert session.scheduled_proactive_reason == ""
    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""
    assert session.scheduled_proactive_interest == 0.0
    assert session.scheduled_proactive_last_check_at is None
    assert session.scheduled_proactive_check_count == 0


def test_session_window_round_trips_through_dict():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )
    session.scheduled_proactive_last_check_at = 1_100.0
    session.scheduled_proactive_check_count = 2

    loaded = NFCSession.from_dict(session.to_dict())

    assert loaded.scheduled_proactive_start_at == 1_000.0
    assert loaded.scheduled_proactive_end_at == 2_000.0
    assert loaded.scheduled_proactive_context == "晚上问问状态"
    assert loaded.scheduled_proactive_interest == 0.8
    assert loaded.scheduled_proactive_last_check_at == 1_100.0
    assert loaded.scheduled_proactive_check_interval == 600.0
    assert loaded.scheduled_proactive_check_count == 2
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: FAIL with `AttributeError: 'NFCSession' object has no attribute 'set_scheduled_proactive_window'`.

- [ ] **Step 3: Add window fields and methods**

In `domain/session_state.py`, add these fields after existing `scheduled_proactive_reason`:

```python
    # 模型预约的主动思考窗口（Unix 时间戳）
    scheduled_proactive_start_at: float | None = None
    scheduled_proactive_end_at: float | None = None
    scheduled_proactive_context: str = ""
    scheduled_proactive_interest: float = 0.0
    scheduled_proactive_last_check_at: float | None = None
    scheduled_proactive_check_interval: float = 600.0
    scheduled_proactive_check_count: int = 0
```

Replace existing `set_scheduled_proactive` method with:

```python
    def set_scheduled_proactive(self, at: float | None, reason: str = "") -> None:
        """设置（或清除）模型预约的主动思考时间。

        Args:
            at: Unix 时间戳，None 表示清除预约
            reason: 预约理由，触发时注入提示词
        """
        self.clear_scheduled_proactive()
        self.scheduled_proactive_at = at
        self.scheduled_proactive_reason = reason if at is not None else ""

    def set_scheduled_proactive_window(
        self,
        start_at: float,
        end_at: float,
        context: str = "",
        interest: float = 1.0,
        check_interval_seconds: float = 600.0,
    ) -> None:
        """设置模型预约的主动思考窗口。"""
        self.clear_scheduled_proactive()
        self.scheduled_proactive_start_at = float(start_at)
        self.scheduled_proactive_end_at = float(end_at)
        self.scheduled_proactive_context = context.strip()
        self.scheduled_proactive_interest = max(0.0, min(1.0, float(interest)))
        self.scheduled_proactive_last_check_at = None
        self.scheduled_proactive_check_interval = max(300.0, min(900.0, float(check_interval_seconds)))
        self.scheduled_proactive_check_count = 0

    def clear_scheduled_proactive(self) -> None:
        """清除单点或窗口主动思考预约。"""
        self.scheduled_proactive_at = None
        self.scheduled_proactive_reason = ""
        self.scheduled_proactive_start_at = None
        self.scheduled_proactive_end_at = None
        self.scheduled_proactive_context = ""
        self.scheduled_proactive_interest = 0.0
        self.scheduled_proactive_last_check_at = None
        self.scheduled_proactive_check_interval = 600.0
        self.scheduled_proactive_check_count = 0
```

In `to_dict`, after `scheduled_proactive_reason`, add:

```python
            "scheduled_proactive_start_at": self.scheduled_proactive_start_at,
            "scheduled_proactive_end_at": self.scheduled_proactive_end_at,
            "scheduled_proactive_context": self.scheduled_proactive_context,
            "scheduled_proactive_interest": self.scheduled_proactive_interest,
            "scheduled_proactive_last_check_at": self.scheduled_proactive_last_check_at,
            "scheduled_proactive_check_interval": self.scheduled_proactive_check_interval,
            "scheduled_proactive_check_count": self.scheduled_proactive_check_count,
```

In `from_dict`, after loading `scheduled_proactive_reason`, add:

```python
        session.scheduled_proactive_start_at = data.get("scheduled_proactive_start_at")
        session.scheduled_proactive_end_at = data.get("scheduled_proactive_end_at")
        session.scheduled_proactive_context = str(data.get("scheduled_proactive_context", "") or "")
        session.scheduled_proactive_interest = float(data.get("scheduled_proactive_interest", 0.0) or 0.0)
        session.scheduled_proactive_last_check_at = data.get("scheduled_proactive_last_check_at")
        session.scheduled_proactive_check_interval = float(
            data.get("scheduled_proactive_check_interval", 600.0) or 600.0
        )
        session.scheduled_proactive_check_count = int(
            data.get("scheduled_proactive_check_count", 0) or 0
        )
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domain/session_state.py tests/test_proactive_window.py
git commit -m "feat: add proactive window session state"
```

---

### Task 2: Parse and apply absolute schedule windows

**Files:**
- Modify: `domain/decision.py`
- Modify: `protocol/decision_parser.py`
- Modify: `services/proactive_service.py`
- Modify: `actions/schedule_proactive.py`
- Modify: `config.py`
- Test: `tests/test_proactive_window.py`

- [ ] **Step 1: Add failing tests for parser and service**

Append to `tests/test_proactive_window.py`:

```python
from dataclasses import dataclass

from neo_fatum_chatter.domain.decision import ProactiveSchedule
from neo_fatum_chatter.protocol.decision_parser import build_decision
from neo_fatum_chatter.services.proactive_service import ProactiveService


@dataclass
class _Call:
    name: str
    args: dict
    id: str = "call-1"


class _Response:
    def __init__(self, calls):
        self.tool_calls = calls


class _Result:
    thought = ""
    mood = ""
    expected_reaction = ""
    max_wait_seconds = 0.0
    actions = []
    has_reply = False
    has_do_nothing = False
    has_meaningful_action = True
    has_info_tool = False


def test_decision_parser_extracts_schedule_window():
    response = _Response([
        _Call(
            name="schedule_proactive",
            args={
                "start_at": "2026-05-30 20:00",
                "end_at": "2026-05-30 22:00",
                "context": "晚上问问状态",
                "interest": 0.9,
            },
        )
    ])

    decision = build_decision(_Result(), response)

    assert decision.proactive_schedule is not None
    assert decision.proactive_schedule.start_at == "2026-05-30 20:00"
    assert decision.proactive_schedule.end_at == "2026-05-30 22:00"
    assert decision.proactive_schedule.context == "晚上问问状态"
    assert decision.proactive_schedule.interest == 0.9


def test_proactive_service_applies_absolute_window(monkeypatch):
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    ProactiveService.apply_schedule(
        session,
        ProactiveSchedule(
            start_at="2026-05-30 20:00",
            end_at="2026-05-30 22:00",
            context="晚上问问状态",
            interest=0.9,
        ),
        now_ts=1_000.0,
    )

    assert session.scheduled_proactive_start_at is not None
    assert session.scheduled_proactive_end_at is not None
    assert session.scheduled_proactive_end_at > session.scheduled_proactive_start_at
    assert session.scheduled_proactive_context == "晚上问问状态"
    assert session.scheduled_proactive_interest == 0.9
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: FAIL because `ProactiveSchedule` has no `start_at`/`end_at` fields and `apply_schedule` has no `now_ts` parameter.

- [ ] **Step 3: Extend ProactiveSchedule dataclass**

In `domain/decision.py`, replace `ProactiveSchedule` with:

```python
@dataclass(slots=True)
class ProactiveSchedule:
    """模型预约的下一次主动发起计划。"""

    delay_minutes: float | None = None
    reason: str = ""
    start_at: str = ""
    end_at: str = ""
    context: str = ""
    interest: float = 1.0
```

- [ ] **Step 4: Parse window arguments**

In `protocol/decision_parser.py`, replace the `if normalized_name == "schedule_proactive":` block with:

```python
        if normalized_name == "schedule_proactive":
            delay_raw = args.get("delay_minutes")
            delay_minutes: float | None
            if delay_raw is None:
                delay_minutes = None
            else:
                try:
                    delay_minutes = float(delay_raw)
                except (TypeError, ValueError):
                    delay_minutes = 30.0
            interest_raw = args.get("interest", 1.0)
            try:
                interest = float(interest_raw)
            except (TypeError, ValueError):
                interest = 1.0
            proactive_schedule = ProactiveSchedule(
                delay_minutes=delay_minutes,
                reason=str(args.get("reason", "") or "").strip(),
                start_at=str(args.get("start_at", "") or "").strip(),
                end_at=str(args.get("end_at", "") or "").strip(),
                context=str(args.get("context", "") or "").strip(),
                interest=max(0.0, min(1.0, interest)),
            )
```

- [ ] **Step 5: Add config fields**

In `config.py`, inside `ProactiveSection`, after `check_interval`, add:

```python
        window_check_interval_min: int = Field(
            default=300, description="预约窗口内 sub-actor 检查最小间隔(秒)"
        )
        window_check_interval_max: int = Field(
            default=900, description="预约窗口内 sub-actor 检查最大间隔(秒)"
        )
        window_interest_decay: float = Field(
            default=0.35, description="主 actor 被唤醒但未回复时的兴趣衰减倍率"
        )
        window_interest_floor: float = Field(
            default=0.1, description="预约窗口兴趣值下限"
        )
        window_max_attempts: int = Field(
            default=3, description="单个预约窗口最多唤醒主 actor 的次数"
        )
```

- [ ] **Step 6: Implement absolute time parsing and application**

In `services/proactive_service.py`, replace the file content with:

```python
"""NFC 主动思考服务。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from ..domain.decision import ProactiveSchedule
    from ..session import NFCSession


logger = get_logger("NFC_proactive_service")


class ProactiveService:
    """处理主动思考预约的 session 副作用。"""

    @staticmethod
    def apply_schedule(
        session: NFCSession,
        proactive_schedule: ProactiveSchedule,
        now_ts: float | None = None,
    ) -> None:
        """根据决策结果更新主动思考预约。"""
        now = time.time() if now_ts is None else now_ts
        if proactive_schedule.start_at and proactive_schedule.end_at:
            start_ts = ProactiveService._parse_absolute_time(proactive_schedule.start_at)
            end_ts = ProactiveService._parse_absolute_time(proactive_schedule.end_at)
            if end_ts <= start_ts:
                raise ValueError("schedule_proactive end_at must be later than start_at")
            if end_ts <= now:
                raise ValueError("schedule_proactive window is already expired")
            start_ts = max(start_ts, now + 30 * 60)
            check_interval = 600.0
            session.set_scheduled_proactive_window(
                start_at=start_ts,
                end_at=end_ts,
                context=proactive_schedule.context or proactive_schedule.reason,
                interest=proactive_schedule.interest,
                check_interval_seconds=check_interval,
            )
            logger.info(
                f"[NFC] 已预约主动思考窗口: "
                f"{datetime.fromtimestamp(start_ts).strftime('%Y-%m-%d %H:%M')} - "
                f"{datetime.fromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M')}"
            )
            return

        delay_minutes = proactive_schedule.delay_minutes
        if delay_minutes == 0:
            session.clear_scheduled_proactive()
            logger.info("[NFC] 已取消主动思考预约")
            return

        if delay_minutes is None:
            delay_minutes = 30.0
        delay_minutes = max(30.0, min(1440.0, float(delay_minutes)))
        reason = proactive_schedule.reason
        session.set_scheduled_proactive(
            now + delay_minutes * 60,
            reason=reason,
        )
        logger.info(
            f"[NFC] 已预约主动思考: {delay_minutes:.0f} 分钟后"
            + (f"，理由：{reason}" if reason else "")
        )

    @staticmethod
    def _parse_absolute_time(value: str) -> float:
        """解析模型输出的 YYYY-MM-DD HH:mm 绝对时间。"""
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").timestamp()

    @staticmethod
    def decay_window_interest(
        session: NFCSession,
        decay: float,
        floor: float,
    ) -> None:
        """主 actor 被唤醒但未回复时降低窗口兴趣值。"""
        if session.scheduled_proactive_start_at is None:
            return
        current = session.scheduled_proactive_interest or 0.0
        session.scheduled_proactive_interest = max(float(floor), current * float(decay))
```

- [ ] **Step 7: Update action schema signature**

In `actions/schedule_proactive.py`, change `execute` signature to include new parameters after `reason`:

```python
        start_at: Annotated[
            str,
            "预约窗口开始时间，格式 YYYY-MM-DD HH:mm。与 end_at 同时提供时优先使用时间窗口。",
        ] = "",
        end_at: Annotated[
            str,
            "预约窗口结束时间，格式 YYYY-MM-DD HH:mm。必须晚于 start_at。",
        ] = "",
        context: Annotated[
            str,
            "预约窗口触发时要带给未来自己的上下文。说明为什么那段时间可能想联系对方。",
        ] = "",
        interest: Annotated[
            float,
            "预约兴趣值，0~1。越高越可能在窗口内触发 sub-actor 判断。",
        ] = 1.0,
```

Then inside `execute`, before `if delay_minutes == 0`, add:

```python
        if start_at and end_at:
            return True, f"已预约在 {start_at} 到 {end_at} 之间主动思考"
```

Also update `_BASE_DESCRIPTION` to mention:

```python
"也可以使用 start_at/end_at 预约一个时间窗口，格式为 YYYY-MM-DD HH:mm。窗口触发时系统会把 context 注入给未来的你。"
```

- [ ] **Step 8: Run tests and verify pass**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add actions/schedule_proactive.py config.py domain/decision.py protocol/decision_parser.py services/proactive_service.py tests/test_proactive_window.py
git commit -m "feat: parse proactive schedule windows"
```

---

### Task 3: Implement background sub-actor window checks

**Files:**
- Modify: `thinker/proactive.py`
- Test: `tests/test_proactive_window.py`

- [ ] **Step 1: Add failing tests for window checks**

Append to `tests/test_proactive_window.py`:

```python
import asyncio

from neo_fatum_chatter.thinker.proactive import ProactiveThinker


class _ProactiveConfig:
    enabled = True
    silence_threshold = 7200
    trigger_probability = 0.0
    min_interval = 1800
    quiet_hours_start = "23:00"
    quiet_hours_end = "07:00"
    check_interval = 60
    window_max_attempts = 3


class _Config:
    proactive = _ProactiveConfig()


class _Store:
    def __init__(self, sessions):
        self._sessions = sessions

    def get_all_cached(self):
        return self._sessions

    async def list_all_stream_ids(self):
        return list(self._sessions)

    async def peek(self, stream_id):
        return self._sessions.get(stream_id)


async def _always_true(*args, **kwargs):
    return True


async def _always_false(*args, **kwargs):
    return False


def test_window_check_triggers_when_sub_actor_accepts(monkeypatch):
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )
    thinker = ProactiveThinker(_Config(), _Store({"s1": session}))
    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_true)

    triggered = asyncio.run(thinker.check_all_sessions())

    assert triggered == ["s1"]
    assert session.scheduled_proactive_last_check_at == 1_100.0
    assert session.scheduled_proactive_check_count == 1


def test_window_check_skips_when_sub_actor_rejects(monkeypatch):
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )
    thinker = ProactiveThinker(_Config(), _Store({"s1": session}))
    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_false)

    triggered = asyncio.run(thinker.check_all_sessions())

    assert triggered == []
    assert session.scheduled_proactive_last_check_at == 1_100.0
    assert session.scheduled_proactive_check_count == 1
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: FAIL because `ProactiveThinker` does not handle window fields and has no `_ask_sub_actor`.

- [ ] **Step 3: Add window checking methods**

In `thinker/proactive.py`, add imports:

```python
import asyncio
```

In `_check_and_trigger`, before single-point `scheduled_proactive_at` logic, add:

```python
        if session.scheduled_proactive_start_at is not None:
            return await self._check_window(stream_id, session, now)
```

Add methods inside `ProactiveThinker`:

```python
    async def _check_window(self, stream_id: str, session: NFCSession, now: float) -> bool:
        """检查预约窗口是否应唤醒主 actor。"""
        start_at = session.scheduled_proactive_start_at
        end_at = session.scheduled_proactive_end_at
        if start_at is None or end_at is None:
            return False
        if now < start_at:
            return False
        if now > end_at:
            session.clear_scheduled_proactive()
            return False
        max_attempts = getattr(self._config.proactive, "window_max_attempts", 3)
        if session.scheduled_proactive_check_count >= max_attempts:
            return False
        last_check = session.scheduled_proactive_last_check_at
        interval = session.scheduled_proactive_check_interval
        if last_check is not None and now - last_check < interval:
            return False
        if random.random() > max(0.0, min(1.0, session.scheduled_proactive_interest)):
            return False

        session.scheduled_proactive_last_check_at = now
        session.scheduled_proactive_check_count += 1
        should_wake = await self._ask_sub_actor(stream_id, session, now)
        if should_wake:
            logger.info(f"主动思考窗口：sub-actor 建议唤醒 stream={stream_id[:8]}")
        return should_wake

    async def _ask_sub_actor(self, stream_id: str, session: NFCSession, now: float) -> bool:
        """后台 sub-actor 判断当前是否值得唤醒主 actor。"""
        _ = stream_id, now
        await asyncio.sleep(0)
        return bool(session.scheduled_proactive_context)
```

- [ ] **Step 4: Handle disk-only window sessions**

In `check_all_sessions`, replace disk-only block:

```python
            if session.scheduled_proactive_at is not None:
                now = time.time()
                if now >= session.scheduled_proactive_at:
                    logger.info(f"主动思考（磁盘 session）：触发预约 stream={stream_id[:8]}")
                    triggered.append(stream_id)
```

with:

```python
            now = time.time()
            if session.scheduled_proactive_start_at is not None:
                if await self._check_window(stream_id, session, now):
                    logger.info(f"主动思考（磁盘 session）：触发预约窗口 stream={stream_id[:8]}")
                    triggered.append(stream_id)
                continue
            if session.scheduled_proactive_at is not None and now >= session.scheduled_proactive_at:
                logger.info(f"主动思考（磁盘 session）：触发预约 stream={stream_id[:8]}")
                triggered.append(stream_id)
```

- [ ] **Step 5: Run tests and verify pass**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add thinker/proactive.py tests/test_proactive_window.py
git commit -m "feat: check proactive windows in background"
```

---

### Task 4: Preserve window context and decay interest after no reply

**Files:**
- Modify: `thinker/proactive.py`
- Modify: `plugin.py`
- Modify: `runtime/orchestrator.py`
- Modify: `handlers/proactive_handler.py`
- Modify: `prompts/modules.py`
- Modify: `services/proactive_service.py`
- Test: `tests/test_proactive_window.py`

- [ ] **Step 1: Add failing tests for interest decay**

Append to `tests/test_proactive_window.py`:

```python

def test_decay_window_interest_uses_floor():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )

    ProactiveService.decay_window_interest(session, decay=0.35, floor=0.1)
    assert session.scheduled_proactive_interest == 0.35

    ProactiveService.decay_window_interest(session, decay=0.1, floor=0.1)
    assert session.scheduled_proactive_interest == 0.1
```

- [ ] **Step 2: Run tests and verify current behavior**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS if Task 2 already added `decay_window_interest`. If it fails, add the exact method from Task 2 Step 6.

- [ ] **Step 3: Pass full window context through event**

In `thinker/proactive.py`, change `mark_triggered` return type and body.

Replace signature:

```python
    async def mark_triggered(self, stream_id: str) -> str:
```

with:

```python
    async def mark_triggered(self, stream_id: str) -> dict[str, object]:
```

Replace method body with:

```python
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get(stream_id)
            if session:
                payload: dict[str, object] = {
                    "scheduled_reason": session.scheduled_proactive_reason,
                    "scheduled_context": session.scheduled_proactive_context,
                    "scheduled_start_at": session.scheduled_proactive_start_at,
                    "scheduled_end_at": session.scheduled_proactive_end_at,
                    "scheduled_interest": session.scheduled_proactive_interest,
                    "from_window": session.scheduled_proactive_start_at is not None,
                }
                session.last_proactive_at = time.time()
                if session.scheduled_proactive_start_at is None:
                    session.clear_scheduled_proactive()
                await self._session_store.save(session)
                return payload
        return {}
```

In `plugin.py`, replace:

```python
                    scheduled_reason = await proactive.mark_triggered(stream_id)
```

with:

```python
                    proactive_payload = await proactive.mark_triggered(stream_id)
```

and replace event params:

```python
                        {"stream_id": stream_id, "scheduled_reason": scheduled_reason},
```

with:

```python
                        {"stream_id": stream_id, **proactive_payload},
```

- [ ] **Step 4: Inject window context into handler and prompt**

In `handlers/proactive_handler.py`, replace:

```python
        scheduled_reason: str = params.get("scheduled_reason", "")
```

with:

```python
        scheduled_reason: str = params.get("scheduled_reason", "")
        scheduled_context: str = params.get("scheduled_context", "")
        scheduled_start_at = params.get("scheduled_start_at")
        scheduled_end_at = params.get("scheduled_end_at")
```

Change `_wake_stream` call to:

```python
            success = await self._wake_stream(
                stream_id,
                scheduled_reason,
                scheduled_context=scheduled_context,
                scheduled_start_at=scheduled_start_at,
                scheduled_end_at=scheduled_end_at,
            )
```

Change `_wake_stream` signature to:

```python
    async def _wake_stream(
        self,
        stream_id: str,
        scheduled_reason: str = "",
        scheduled_context: str = "",
        scheduled_start_at: float | None = None,
        scheduled_end_at: float | None = None,
    ) -> bool:
```

Change `build_proactive_context` call to:

```python
            proactive_content = await build_proactive_context(
                silence_minutes=silence_minutes,
                recent_activity=recent_activity,
                scheduled_reason=scheduled_reason,
                scheduled_context=scheduled_context,
                scheduled_start_at=scheduled_start_at,
                scheduled_end_at=scheduled_end_at,
                use_tool_calling=use_tool_calling,
            )
```

In `prompts/modules.py`, change `build_proactive_context` signature to:

```python
async def build_proactive_context(
    silence_minutes: float,
    recent_activity: str,
    scheduled_reason: str = "",
    scheduled_context: str = "",
    scheduled_start_at: float | None = None,
    scheduled_end_at: float | None = None,
    use_tool_calling: bool = True,
) -> str:
```

Before `return result`, replace existing scheduled_reason prepend block with:

```python
    schedule_lines: list[str] = []
    if scheduled_reason:
        schedule_lines.append(f"预约理由：{scheduled_reason}")
    if scheduled_context:
        schedule_lines.append(f"预约上下文：{scheduled_context}")
    if scheduled_start_at is not None and scheduled_end_at is not None:
        start_text = datetime.datetime.fromtimestamp(float(scheduled_start_at)).strftime("%Y-%m-%d %H:%M")
        end_text = datetime.datetime.fromtimestamp(float(scheduled_end_at)).strftime("%Y-%m-%d %H:%M")
        schedule_lines.append(f"预约窗口：{start_text} - {end_text}")
    if schedule_lines:
        result = "【你之前为这次主动发起做了预约：" + "；".join(schedule_lines) + "】\n\n" + result
```

- [ ] **Step 5: Notify service after main actor decision**

In `services/proactive_service.py`, add:

```python
    @staticmethod
    def handle_window_actor_result(
        session: NFCSession,
        replied: bool,
        decay: float,
        floor: float,
    ) -> None:
        """处理预约窗口唤醒后的主 actor 决策结果。"""
        if session.scheduled_proactive_start_at is None:
            return
        if replied:
            session.clear_scheduled_proactive()
            return
        ProactiveService.decay_window_interest(session, decay=decay, floor=floor)
```

In `runtime/orchestrator.py`, after `commit_turn_decision(...)` returns `turn_control`, add:

```python
        if getattr(trigger_msg, "metadata", {}).get("nfc_proactive_window"):
            decay = getattr(config.proactive, "window_interest_decay", 0.35)
            floor = getattr(config.proactive, "window_interest_floor", 0.1)
            ProactiveService.handle_window_actor_result(
                session,
                replied=decision.has_reply_action,
                decay=decay,
                floor=floor,
            )
```

In `handlers/proactive_handler.py`, when building the trigger message, ensure metadata marks window triggers. After `trigger_message = self._build_proactive_message(...)`, add:

```python
        if scheduled_start_at is not None:
            try:
                trigger_message.metadata["nfc_proactive_window"] = True
            except Exception:
                pass
```

- [ ] **Step 6: Run related tests**

Run:

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS.

- [ ] **Step 7: Run existing tests**

Run:

```bash
pytest tests/test_call_resolver.py tests/test_response_normalizer.py tests/test_reply_executor.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add handlers/proactive_handler.py plugin.py prompts/modules.py runtime/orchestrator.py services/proactive_service.py thinker/proactive.py tests/test_proactive_window.py
git commit -m "feat: preserve proactive window context"
```

---

### Task 5: Verify lint and full test suite

**Files:**
- No code changes expected.

- [ ] **Step 1: Run proactive window tests**

```bash
pytest tests/test_proactive_window.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full tests**

```bash
pytest
```

Expected: PASS.

- [ ] **Step 3: Run ruff on modified source files**

```bash
ruff check actions/schedule_proactive.py config.py domain/decision.py domain/session_state.py handlers/proactive_handler.py plugin.py prompts/modules.py protocol/decision_parser.py runtime/orchestrator.py services/proactive_service.py thinker/proactive.py tests/test_proactive_window.py
```

Expected: PASS.

- [ ] **Step 4: Inspect git diff**

```bash
git diff --stat
git diff -- actions/schedule_proactive.py config.py domain/decision.py domain/session_state.py handlers/proactive_handler.py plugin.py prompts/modules.py protocol/decision_parser.py runtime/orchestrator.py services/proactive_service.py thinker/proactive.py tests/test_proactive_window.py
```

Expected: Only proactive window scheduling changes are present.

---

## Self-review

- Spec coverage: covers absolute `YYYY-MM-DD HH:mm` windows, background sub-actor checks, 5~15 minute cadence, main actor final decision, context injection, and interest decay after no reply.
- Placeholder scan: no TBD/TODO/fill-later placeholders.
- Type consistency: uses `scheduled_proactive_start_at`, `scheduled_proactive_end_at`, `scheduled_proactive_context`, `scheduled_proactive_interest`, `scheduled_proactive_last_check_at`, `scheduled_proactive_check_interval`, and `scheduled_proactive_check_count` consistently across tasks.
- Scope note: this plan intentionally keeps `_ask_sub_actor` minimal first. If the project already has a specific sub-actor LLM API, replace only `_ask_sub_actor` internals while preserving its boolean contract.
