"""日程驱动的现实世界状态（Daily-Life World State）。

设计移植自 AGPL3 的 astrbot_plugin_private_companion（"我会永远陪着你"）
的世界状态内核，按 NFC 的 session/上下文架构重写：

- 五段日程窗口（深夜/早晨/中午/下午/晚上）与跨午夜判定，
  派生自 private_companion 的 ``bot_personal_contract.SCHEDULE_WINDOWS``；
- 作息锚点（起床/入睡分钟数，带平移夹紧），对应其 chronotype 的
  标准锚点 + 有限平移思想；
- 活动条目按来源分级：routine（作息模板，推断）/ planned（当日计划，承诺）/
  commit（角色自述，事实）/ observed（已发生观察，事实）——对应其
  evidence/commitment/epistemic 分级里"计划文本不自动成为当前事实"的语义；
- 当前活动推导：自述/观察等事实级条目优先于计划，计划优先于作息推断，
  作息只在窗口内给出"大概在做什么"的推断，永不冒充事实。

与双登记簿的关系：本模块是**现实登记簿**（reality）的世界模型，
给角色一个"自己在生活"的连续日常；story 登记簿（domain/world.py）
仍是剧情演绎世界，两者通过 active_register 切换，互不污染。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ── 五段日程窗口（移植自 private_companion bot_personal_contract）────────
# 字段：(slug, 中文名, 起始分钟, 结束分钟)。结束 <= 起始表示跨午夜。
SCHEDULE_WINDOWS: tuple[tuple[str, str, int, int], ...] = (
    ("late_night", "深夜", 21 * 60, 6 * 60),        # 21:00 - 次日 06:00
    ("morning", "早晨", 6 * 60, 11 * 60),           # 06:00 - 11:00
    ("noon", "中午", 11 * 60, 14 * 60 + 30),        # 11:00 - 14:30
    ("afternoon", "下午", 14 * 60 + 30, 18 * 60),   # 14:30 - 18:00
    ("evening", "晚上", 18 * 60, 21 * 60),          # 18:00 - 21:00
)

WINDOW_NAME_BY_SLUG: dict[str, str] = {item[0]: item[1] for item in SCHEDULE_WINDOWS}

# 口语别名归一（对应 private_companion 的 WINDOW_ALIASES，取常用子集）
_WINDOW_ALIASES: dict[str, str] = {
    "深夜": "late_night", "凌晨": "late_night", "半夜": "late_night",
    "夜里": "late_night", "早上": "morning", "清晨": "morning",
    "上午": "morning", "早晨": "morning", "中午": "noon", "午间": "noon",
    "下午": "afternoon", "傍晚": "evening", "晚上": "evening", "晚间": "evening",
}

# 活动来源（对应 private_companion 的 source_kind / epistemic 分级）
SOURCE_ROUTINE = "routine"        # 作息模板推断（inferred）
SOURCE_PLANNED = "planned"        # 当日计划（asserted commitment）
SOURCE_COMMIT = "commit"          # 角色自述当前在做（asserted fact）
SOURCE_OBSERVED = "observed"      # 运行时观察到的既成事实（observed fact）

_FACT_SOURCES = frozenset({SOURCE_COMMIT, SOURCE_OBSERVED})

_MAX_PLAN_ITEMS = 12
_MAX_OBSERVATIONS = 8

# Agenda 状态只描述记录的生命周期，不提升事实等级。
STATUS_PLANNED = "planned"
STATUS_CONFIRMED = "confirmed"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
_STATUS_VALUES = frozenset({
    STATUS_PLANNED,
    STATUS_CONFIRMED,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
})

# 默认作息模板：每窗口一条"大概在做什么"（可被 config.world.routine 覆盖）
DEFAULT_ROUTINE: dict[str, str] = {
    "late_night": "睡觉",
    "morning": "洗漱、吃早餐，处理一下上午的安排",
    "noon": "吃午饭，饭后刷刷手机",
    "afternoon": "忙自己的事（上课/工作/杂务）",
    "evening": "放松时间（看剧/打游戏/和朋友聊天）",
}


def window_for_minutes(minutes: int) -> str:
    """把当日分钟数归入窗口 slug（跨午夜窗口拆两截判定）。"""
    try:
        value = int(minutes) % (24 * 60)
    except (TypeError, ValueError):
        return ""
    for slug, _name, start, end in SCHEDULE_WINDOWS:
        if start < end:
            if start <= value < end:
                return slug
        elif value >= start or value < end:
            return slug
    return ""


def window_for_ts(ts: float) -> str:
    """把时间戳归入窗口 slug。"""
    local = time.localtime(float(ts))
    return window_for_minutes(local.tm_hour * 60 + local.tm_min)


def normalize_window(value: Any) -> str:
    """把窗口名/别名/slug 归一成 slug；无法识别返回空串（不猜）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    for slug, _name, _start, _end in SCHEDULE_WINDOWS:
        if text == slug:
            return slug
    return _WINDOW_ALIASES.get(text, "")


def parse_clock(value: Any) -> int:
    """把 "HH:MM" / "H点半" / 分钟数 解析为当日分钟数；失败返回 -1。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes = int(value)
        return minutes if 0 <= minutes < 24 * 60 else -1
    text = str(value or "").strip()
    if not text:
        return -1
    if text.isdigit():
        minutes = int(text)
        return minutes % (24 * 60) if minutes >= 0 else -1
    for sep in (":", "：", "点", "时"):
        if sep in text:
            head, _, tail = text.partition(sep)
            hour = _cn_int(head)
            if hour is None:
                return -1
            tail = tail.replace("分", "").replace("半", "30").strip() or "0"
            minute = _cn_int(tail)
            if minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
                return -1
            return hour * 60 + minute
    return -1


def _cn_int(text: str) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    cn = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "半":
        return 30
    if len(text) == 1 and text in cn:
        return cn[text]
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = cn.get(head, 1) if head else 1
        ones = cn.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


def _minutes_to_clock(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class AgendaItem:
    """一条活动条目（对应 private_companion 的 plan/activity item 精简版）。

    start_minute/end_minute 为当日分钟数，end <= start 表示跨午夜。
    """

    activity: str = ""
    source: str = SOURCE_PLANNED
    start_minute: int = -1
    end_minute: int = -1
    location: str = ""
    note: str = ""
    created_at: float = 0.0
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_ref: str = ""
    idempotency_key: str = ""
    revision: int = 1
    actor: str = "self"
    authority: str = "self_report"
    evidence_kind: str = "asserted"
    evidence_level: str = "L1"
    epistemic_status: str = "inferred"
    materialization_state: str = "virtual"
    temporal_phase: str = "current"
    status: str = STATUS_PLANNED
    expires_at: float | None = None
    day_key: str = ""

    def __post_init__(self) -> None:
        """为旧构造方式补齐保守的来源默认值。"""
        self.activity = str(self.activity or "").strip()[:80]
        if not (-1 <= self.start_minute < 24 * 60):
            self.start_minute = -1
        if not (-1 <= self.end_minute < 24 * 60):
            self.end_minute = -1
        self.source = self.source if self.source in {
            SOURCE_ROUTINE, SOURCE_PLANNED, SOURCE_COMMIT, SOURCE_OBSERVED,
        } else SOURCE_PLANNED
        if self.source == SOURCE_OBSERVED and self.status == STATUS_PLANNED:
            self.status = STATUS_COMPLETED
        elif self.source == SOURCE_COMMIT and self.status == STATUS_PLANNED:
            self.status = STATUS_ACTIVE
        if self.source in _FACT_SOURCES and self.epistemic_status == "inferred":
            self.epistemic_status = "asserted"
        if self.source == SOURCE_OBSERVED and self.evidence_kind == "asserted":
            self.evidence_kind = "observed"
        self.revision = max(1, _safe_int(self.revision, 1))

    @property
    def is_fact(self) -> bool:
        """事实级条目（自述/观察）可直接作为“当前在做什么”。"""
        # completed 的 observed 仍是事实，只是不再是“进行中”的活动。
        return self.source in _FACT_SOURCES and self.status not in {
            STATUS_CANCELLED, STATUS_EXPIRED,
        }

    @property
    def is_active(self) -> bool:
        return self.status in {STATUS_PLANNED, STATUS_CONFIRMED, STATUS_ACTIVE}

    def effective_end_minute(self) -> int:
        """返回用于覆盖/滚动判定的结束分钟；未指定时默认两小时。"""
        if self.start_minute < 0:
            return -1
        if self.end_minute >= 0:
            return self.end_minute
        return (self.start_minute + 120) % (24 * 60)

    def covers(self, minute_of_day: int) -> bool:
        """时间窗口是否覆盖指定分钟（跨午夜环形判定）。"""
        if self.start_minute < 0 or not self.is_active:
            return False
        end = self.effective_end_minute()
        minute_of_day %= 24 * 60
        if self.start_minute == end:
            # 显式相同起止时间代表零时长事件，不是“全天覆盖”。
            return False
        if self.start_minute < end:
            return self.start_minute <= minute_of_day < end
        # 跨午夜
        return minute_of_day >= self.start_minute or minute_of_day < end

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "source": self.source,
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "location": self.location,
            "note": self.note,
            "created_at": self.created_at,
            "item_id": self.item_id,
            "source_ref": self.source_ref,
            "idempotency_key": self.idempotency_key,
            "revision": self.revision,
            "actor": self.actor,
            "authority": self.authority,
            "evidence_kind": self.evidence_kind,
            "evidence_level": self.evidence_level,
            "epistemic_status": self.epistemic_status,
            "materialization_state": self.materialization_state,
            "temporal_phase": self.temporal_phase,
            "status": self.status,
            "expires_at": self.expires_at,
            "day_key": self.day_key,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AgendaItem | None":
        if not isinstance(data, dict):
            return None
        activity = str(data.get("activity", "") or "").strip()
        if not activity:
            return None
        source = str(data.get("source", SOURCE_PLANNED) or SOURCE_PLANNED)
        if source not in {SOURCE_ROUTINE, SOURCE_PLANNED, SOURCE_COMMIT, SOURCE_OBSERVED}:
            source = SOURCE_PLANNED
        status = str(data.get("status", "") or "").strip()
        if status not in _STATUS_VALUES:
            status = STATUS_COMPLETED if source == SOURCE_OBSERVED else (
                STATUS_ACTIVE if source == SOURCE_COMMIT else STATUS_PLANNED
            )
        expires_at = data.get("expires_at")
        expires = _safe_float(expires_at, 0.0) if expires_at is not None else None
        return cls(
            activity=activity[:80],
            source=source,
            start_minute=_safe_int(data.get("start_minute"), -1),
            end_minute=_safe_int(data.get("end_minute"), -1),
            location=str(data.get("location", "") or "").strip()[:40],
            note=str(data.get("note", "") or "").strip()[:120],
            created_at=_safe_float(data.get("created_at"), 0.0),
            item_id=str(data.get("item_id", "") or uuid.uuid4().hex[:12])[:32],
            source_ref=str(data.get("source_ref", "") or "").strip()[:120],
            idempotency_key=str(data.get("idempotency_key", "") or "").strip()[:120],
            revision=max(1, _safe_int(data.get("revision"), 1)),
            actor=str(data.get("actor", "self") or "self").strip()[:40],
            authority=str(data.get("authority", "self_report") or "self_report").strip()[:40],
            evidence_kind=str(data.get("evidence_kind", "asserted") or "asserted").strip()[:40],
            evidence_level=str(data.get("evidence_level", "L1") or "L1").strip()[:16],
            epistemic_status=str(data.get("epistemic_status", "inferred") or "inferred").strip()[:32],
            materialization_state=str(data.get("materialization_state", "virtual") or "virtual").strip()[:32],
            temporal_phase=str(data.get("temporal_phase", "current") or "current").strip()[:32],
            status=status,
            expires_at=expires if expires and expires > 0 else None,
            day_key=str(data.get("day_key", "") or "")[:16],
        )


@dataclass
class DailyLifeState:
    """角色的现实日常世界状态。

    session 字段约定：``session.daily_life``。
    """

    # 作息锚点（当日分钟数）：标准作息 7:30 醒 / 22:30 睡（对齐 chronotype 默认）
    wake_minute: int = 7 * 60 + 30
    sleep_minute: int = 22 * 60 + 30
    # 当前位置（位置连续性：只在有信息时更新，避免臆造）
    location: str = ""
    location_source: str = "persistent"
    location_item_id: str = ""
    location_expires_at: float | None = None
    # 作息模板（窗口 slug -> "此刻大概在做什么"）；空槽回退 DEFAULT_ROUTINE
    routine: dict[str, str] = field(default_factory=dict)
    # 当日计划（planned）与角色自述（commit）
    plan_items: list[AgendaItem] = field(default_factory=list)
    # 运行时观察到的既成事实（observed）
    observations: list[AgendaItem] = field(default_factory=list)
    # 上次按日期键滚动的日子（跨天清空当日性条目）
    _last_day_key: str = ""
    revision: int = 0
    updated_at: float = 0.0

    def _touch(self) -> None:
        self.revision += 1
        self.updated_at = time.time()

    # ── 作息推导 ──────────────────────────────────────────

    def is_asleep(self, ts: float | None = None) -> bool:
        """按作息锚点判断此刻是否应在睡觉（跨午夜区间）。"""
        now_ts = float(ts if ts is not None else time.time())
        local = time.localtime(now_ts)
        minute = local.tm_hour * 60 + local.tm_min
        if self.sleep_minute > self.wake_minute:
            # 常规：入睡锚点在当天、起床锚点在次日（如 22:30 睡 / 7:30 起）
            return minute >= self.sleep_minute or minute < self.wake_minute
        return self.sleep_minute <= minute < self.wake_minute

    def current_window(self, ts: float | None = None) -> str:
        return window_for_ts(float(ts if ts is not None else time.time()))

    def ensure_day(self, ts: float | None = None) -> bool:
        """按本地日期滚动当日条目，返回是否发生了滚动。

        旧记录没有 day_key 时按创建时间推断；无法推断的记录只在首次访问时
        归入当前日，避免一次升级把可用状态全部删掉。
        """
        now_ts = float(ts if ts is not None else time.time())
        day_key = time.strftime("%Y-%m-%d", time.localtime(now_ts))
        if self._last_day_key == day_key:
            self._expire_items(now_ts)
            return False
        previous = self._last_day_key
        current_items: list[AgendaItem] = []
        for item in self.plan_items:
            item_day = item.day_key
            if not item_day:
                created = item.created_at
                if created:
                    item_day = time.strftime("%Y-%m-%d", time.localtime(created))
                else:
                    item_day = previous or day_key
                item.day_key = item_day
            if item_day == day_key:
                current_items.append(item)
                continue
            # 跨午夜活动在次日仍可覆盖到结束时间，只有事实/计划的窗口
            # 还未结束时才保留；无结束时间的临时 commit 不跨日继承。
            kept = False
            effective_end = item.effective_end_minute()
            if effective_end >= 0 and item.start_minute > effective_end:
                try:
                    origin_day = datetime.strptime(item_day, "%Y-%m-%d").date()
                    current_day = datetime.strptime(day_key, "%Y-%m-%d").date()
                    is_next_day = (current_day - origin_day).days == 1
                except ValueError:
                    is_next_day = False
                minute = time.localtime(now_ts).tm_hour * 60 + time.localtime(now_ts).tm_min
                if is_next_day and minute < effective_end and item.covers(minute):
                    current_items.append(item)
                    kept = True
            if not kept and self.location_item_id == item.item_id:
                self._clear_temporary_location()
        self.plan_items = current_items[-_MAX_PLAN_ITEMS:]
        self.observations = [
            item for item in self.observations
            if (item.day_key or time.strftime("%Y-%m-%d", time.localtime(item.created_at or now_ts))) == day_key
        ][-_MAX_OBSERVATIONS:]
        self._last_day_key = day_key
        rolled = previous != day_key
        if rolled:
            self.revision += 1
            self.updated_at = now_ts
        self._expire_items(now_ts)
        return rolled

    def _clear_temporary_location(self) -> None:
        if self.location_source != "persistent":
            self.location = ""
            self.location_source = "persistent"
            self.location_item_id = ""
            self.location_expires_at = None

    def _expire_items(self, now_ts: float) -> None:
        """将显式结束或设置 expires_at 的条目标记为过期。"""
        local = time.localtime(now_ts)
        minute = local.tm_hour * 60 + local.tm_min
        changed = False
        current_day = time.strftime("%Y-%m-%d", local)
        if self.location_expires_at is not None and now_ts >= self.location_expires_at:
            self.location = ""
            self.location_source = "persistent"
            self.location_item_id = ""
            self.location_expires_at = None
            changed = True
        for item in [*self.plan_items, *self.observations]:
            if not item.is_active:
                continue
            if item.day_key and item.day_key != current_day:
                try:
                    item_day = datetime.strptime(item.day_key, "%Y-%m-%d").date()
                    now_day = datetime.strptime(current_day, "%Y-%m-%d").date()
                    day_delta = (now_day - item_day).days
                except ValueError:
                    day_delta = 2
                effective_end = item.effective_end_minute()
                overnight_active = (
                    day_delta == 1
                    and item.start_minute > effective_end >= 0
                    and minute < effective_end
                )
                if day_delta > 0 and not overnight_active:
                    item.status = STATUS_EXPIRED
                    item.revision += 1
                    changed = True
                    if self.location_item_id == item.item_id:
                        self.location = ""
                        self.location_source = "persistent"
                        self.location_item_id = ""
                        self.location_expires_at = None
                    continue
            if item.expires_at is not None and now_ts >= item.expires_at:
                item.status = STATUS_EXPIRED
                item.revision += 1
                changed = True
                if self.location_item_id == item.item_id:
                    self.location = ""
                    self.location_source = "persistent"
                    self.location_item_id = ""
                    self.location_expires_at = None
                continue
            if item.end_minute >= 0 and item.source in _FACT_SOURCES:
                # 只在明确经过结束点后收口，不能把“尚未开始”的条目误判为过期。
                # start > end 表示跨午夜：同一自然日还没到 start，不能按 end 倒推过期。
                finished = (
                    item.start_minute < item.end_minute
                    and minute >= item.end_minute
                    and now_ts >= (item.created_at or 0.0)
                )
                if not finished and item.start_minute > item.end_minute and item.day_key:
                    try:
                        item_day = datetime.strptime(item.day_key, "%Y-%m-%d").date()
                        now_day = datetime.strptime(current_day, "%Y-%m-%d").date()
                        finished = (now_day - item_day).days == 1 and minute >= item.end_minute
                    except ValueError:
                        finished = False
                if finished:
                    item.status = STATUS_EXPIRED
                    item.revision += 1
                    changed = True
                    if self.location_item_id == item.item_id:
                        self._clear_temporary_location()
        if changed:
            self.revision += 1
            self.updated_at = now_ts

    # ── 条目维护 ──────────────────────────────────────────

    def add_plan(
        self,
        activity: str,
        *,
        start: Any = None,
        end: Any = None,
        location: str = "",
        note: str = "",
        source_ref: str = "",
        idempotency_key: str = "",
    ) -> AgendaItem | None:
        """添加一条当日计划（asserted commitment）。"""
        activity = (activity or "").strip()
        if not activity:
            return None
        self.ensure_day()
        if idempotency_key:
            existing = self.find_by_idempotency(idempotency_key)
            if existing is not None:
                return existing
        start_minute = _parse_schedule_clock(start) if start is not None else -1
        end_minute = _parse_schedule_clock(end) if end is not None else -1
        if start is not None and start_minute < 0:
            return None
        if end is not None and end_minute < 0:
            return None
        created_at = time.time()
        item = AgendaItem(
            activity=activity[:80],
            source=SOURCE_PLANNED,
            start_minute=start_minute,
            end_minute=end_minute,
            location=(location or "").strip()[:40],
            note=(note or "").strip()[:120],
            created_at=created_at,
            source_ref=source_ref.strip()[:120],
            idempotency_key=idempotency_key.strip()[:120],
            evidence_kind="schedule",
            evidence_level="L0",
            epistemic_status="planned",
            materialization_state="candidate",
            temporal_phase="planned",
            status=STATUS_PLANNED,
            day_key=time.strftime("%Y-%m-%d", time.localtime(created_at)),
        )
        self.plan_items.append(item)
        self.plan_items = self.plan_items[-_MAX_PLAN_ITEMS:]
        self._touch()
        return item

    def commit_activity(
        self,
        activity: str,
        *,
        end: Any = None,
        location: str = "",
    ) -> AgendaItem | None:
        """角色自述"我现在在做 X"（self_state_commit，事实级）。"""
        activity = (activity or "").strip()
        if not activity:
            return None
        end_minute = _parse_schedule_clock(end) if end is not None else -1
        if end is not None and end_minute < 0:
            return None
        self.ensure_day()
        # 新自述取代旧自述：同来源只保留最新一条
        self.plan_items = [
            item for item in self.plan_items if item.source != SOURCE_COMMIT
        ]
        local = time.localtime()
        created_at = time.time()
        item = AgendaItem(
            activity=activity[:80],
            source=SOURCE_COMMIT,
            start_minute=local.tm_hour * 60 + local.tm_min,
            end_minute=end_minute,
            location=(location or "").strip()[:40],
            created_at=created_at,
            authority="self_report",
            evidence_kind="self_state_commit",
            evidence_level="L1",
            epistemic_status="asserted",
            materialization_state="materialized",
            temporal_phase="current",
            status=STATUS_ACTIVE,
            expires_at=(
                _end_time_as_timestamp(end, created_at)
                if end is not None and end_minute >= 0 else None
            ),
            day_key=time.strftime("%Y-%m-%d", time.localtime(created_at)),
        )
        self.plan_items.append(item)
        self.plan_items = self.plan_items[-_MAX_PLAN_ITEMS:]
        if item.location:
            self.location = item.location
            self.location_source = "self_state"
            self.location_item_id = item.item_id
            self.location_expires_at = item.expires_at
        elif self.location_source == "self_state":
            self.location = ""
            self.location_source = "persistent"
            self.location_item_id = ""
            self.location_expires_at = None
        self._touch()
        return item

    def add_observation(
        self,
        activity: str,
        *,
        location: str = "",
        source_ref: str = "",
        idempotency_key: str = "",
    ) -> AgendaItem | None:
        """记录一条已发生的观察事实（observed）。"""
        activity = (activity or "").strip()
        if not activity:
            return None
        self.ensure_day()
        if idempotency_key:
            existing = self.find_by_idempotency(idempotency_key)
            if existing is not None:
                return existing
        created_at = time.time()
        local = time.localtime(created_at)
        item = AgendaItem(
            activity=activity[:80],
            source=SOURCE_OBSERVED,
            start_minute=local.tm_hour * 60 + local.tm_min,
            end_minute=-1,
            location=(location or "").strip()[:40],
            created_at=created_at,
            source_ref=source_ref.strip()[:120],
            idempotency_key=idempotency_key.strip()[:120],
            authority="runtime_observation",
            evidence_kind="observed",
            evidence_level="L2",
            epistemic_status="observed",
            materialization_state="materialized",
            temporal_phase="past",
            status=STATUS_COMPLETED,
            day_key=time.strftime("%Y-%m-%d", time.localtime(created_at)),
            expires_at=created_at + 2 * 3600,
        )
        self.observations.append(item)
        self.observations = self.observations[-_MAX_OBSERVATIONS:]
        if item.location:
            self.location = item.location
            self.location_source = "observation"
            self.location_item_id = item.item_id
            self.location_expires_at = item.expires_at
        self._touch()
        return item

    def find_item(self, item_id: str) -> AgendaItem | None:
        """按稳定 ID 查找日程条目。"""
        wanted = str(item_id or "").strip()
        if not wanted:
            return None
        return next(
            (item for item in [*self.plan_items, *self.observations]
             if item.item_id == wanted),
            None,
        )

    def find_by_idempotency(self, key: str) -> AgendaItem | None:
        wanted = str(key or "").strip()
        if not wanted:
            return None
        return next(
            (item for item in [*self.plan_items, *self.observations]
             if item.idempotency_key == wanted),
            None,
        )

    def reconcile_observation(
        self, planned_item_id: str, observation: AgendaItem
    ) -> AgendaItem | None:
        """用带明确引用的 observed 事实收口原计划，但不提升计划事实等级。"""
        planned = self.find_item(planned_item_id)
        if (
            planned is None
            or planned.source != SOURCE_PLANNED
            or observation.source != SOURCE_OBSERVED
            or observation.source_ref != planned.item_id
        ):
            return None
        if planned.status in {STATUS_CANCELLED, STATUS_EXPIRED}:
            return None
        planned.status = STATUS_COMPLETED
        planned.temporal_phase = "past"
        planned.materialization_state = "reconciled"
        planned.revision += 1
        self._touch()
        return planned

    def update_item_status(self, item_id: str, status: str, *, reason: str = "") -> AgendaItem | None:
        """执行保守的显式状态转换，不改变来源事实等级。"""
        self.ensure_day()
        item = self.find_item(item_id)
        if item is None or status not in _STATUS_VALUES:
            return None
        allowed: dict[str, set[str]] = {
            STATUS_PLANNED: {STATUS_CONFIRMED, STATUS_ACTIVE, STATUS_CANCELLED, STATUS_EXPIRED},
            STATUS_CONFIRMED: {STATUS_ACTIVE, STATUS_CANCELLED, STATUS_EXPIRED},
            STATUS_ACTIVE: {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED},
            STATUS_COMPLETED: set(),
            STATUS_CANCELLED: set(),
            STATUS_EXPIRED: set(),
        }
        if status != item.status and status not in allowed.get(item.status, set()):
            return None
        if status == item.status:
            return item
        item.status = status
        item.revision += 1
        if reason:
            item.note = reason.strip()[:120]
        if status in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED}:
            item.temporal_phase = "past"
            if self.location_item_id == item.item_id:
                self._clear_temporary_location()
        elif status == STATUS_ACTIVE:
            item.temporal_phase = "current"
        self._touch()
        return item

    def clear_commit(self) -> bool:
        """清除当前自述（角色表示已经做完/状态失效）。"""
        before = len(self.plan_items)
        self.plan_items = [
            item for item in self.plan_items if item.source != SOURCE_COMMIT
        ]
        changed = len(self.plan_items) < before
        if changed:
            if self.location_source == "self_state":
                self._clear_temporary_location()
            self._touch()
        return changed

    # ── 当前活动推导（对应 _agenda_current_context_item 语义）────────

    def current_activity(self, ts: float | None = None) -> dict[str, Any] | None:
        """推导"此刻在做什么"。

        优先级：事实级条目（自述/观察，未过期）> 覆盖当前时刻的计划
        > 作息推断（睡觉/窗口模板）。返回 dict 带 epistemic 标记，
        空闲无信息时返回 None（渲染层据此省略，不制造占位文本）。
        """
        now_ts = float(ts if ts is not None else time.time())
        local = time.localtime(now_ts)
        minute = local.tm_hour * 60 + local.tm_min

        # 1) 事实级：最新自述（未过期）
        self.ensure_day(now_ts)

        # 1) 事实级：最新自述（未过期）
        for item in reversed(self.plan_items):
            if item.source != SOURCE_COMMIT or not item.is_fact:
                continue
            # self_state_commit 是“当前持续状态”：查询略早于写入时刻的
            # 回放/渲染也应沿用它；只有明确 expires_at 或结束点已过才失效。
            if item.expires_at is not None and now_ts >= item.expires_at:
                continue
            if item.end_minute >= 0:
                if item.expires_at is not None or item.start_minute != item.end_minute:
                    if item.start_minute < item.end_minute and minute >= item.end_minute:
                        continue
                    if item.start_minute > item.end_minute and item.end_minute <= minute < item.start_minute:
                        continue
            return self._view(item, "fact", now_ts)
        # 2) 事实级：最近的观察（2 小时内）
        for item in reversed(self.observations):
            if item.source == SOURCE_OBSERVED and item.is_fact and item.temporal_phase == "past" and now_ts - (item.created_at or 0) <= 2 * 3600:
                return self._view(item, "fact", now_ts)
        # 3) 计划：覆盖当前时刻的既定安排
        for item in reversed(self.plan_items):
            if item.source == SOURCE_PLANNED and item.is_active and item.covers(minute):
                return self._view(item, "committed", now_ts)
        # 4) 作息推断
        if self.is_asleep(now_ts):
            return {
                "activity": "睡觉",
                "epistemic": "inferred",
                "source": SOURCE_ROUTINE,
                "window": self.current_window(now_ts),
                "location": "",
            }
        window = self.current_window(now_ts)
        if window == "late_night":
            # 醒着的深夜：介于入睡锚点与窗口语义之间，按睡前活动推断
            return {
                "activity": "还没睡，在做睡前的事",
                "epistemic": "inferred",
                "source": SOURCE_ROUTINE,
                "window": window,
                "location": "",
            }
        routine = (self.routine or DEFAULT_ROUTINE).get(window, "") or DEFAULT_ROUTINE.get(window, "")
        if routine:
            return {
                "activity": routine,
                "epistemic": "inferred",
                "source": SOURCE_ROUTINE,
                "window": window,
                "location": "",
            }
        return None

    @staticmethod
    def _view(item: AgendaItem, epistemic: str, now_ts: float) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "activity": item.activity,
            "epistemic": epistemic,
            "source": item.source,
            "status": item.status,
            "window": window_for_ts(now_ts),
            "location": item.location,
            "evidence_kind": item.evidence_kind,
            "evidence_level": item.evidence_level,
            "epistemic_status": item.epistemic_status,
            "revision": item.revision,
        }

    def today_plan_view(self, ts: float | None = None, *, max_items: int = 5) -> list[str]:
        """渲染当日计划（未来视角，对齐 _agenda_context_for_prompt 的过滤语义）。"""
        now_ts = float(ts if ts is not None else time.time())
        self.ensure_day(now_ts)
        local = time.localtime(now_ts)
        minute = local.tm_hour * 60 + local.tm_min
        lines: list[str] = []
        for item in self.plan_items:
            if item.source != SOURCE_PLANNED or not item.is_active:
                continue
            if item.start_minute < 0:
                continue
            end_minute = item.end_minute if item.end_minute >= 0 else (item.start_minute + 120) % (24 * 60)
            if not item.covers(minute) and item.start_minute < minute and end_minute < minute:
                continue  # 已结束的计划不再展示
            clock = _minutes_to_clock(item.start_minute)
            end_clock = _minutes_to_clock(end_minute) if item.end_minute >= 0 else ""
            time_text = f"{clock}-{end_clock}" if end_clock else f"{clock} 起"
            location_text = f"（{item.location}）" if item.location else ""
            lines.append(f"- {time_text} {item.activity}{location_text}")
        return lines[-max_items:]

    # ── 渲染 ──────────────────────────────────────────────

    def render_world_text(self, ts: float | None = None) -> str:
        """渲染现实登记簿的世界状态块。

        中性时返回空串（无位置、无计划、作息推断也无需说明时不输出），
        避免每轮千篇一律的占位文本。
        """
        now_ts = float(ts if ts is not None else time.time())
        current = self.current_activity(now_ts)
        if current is None:
            return ""

        epistemic = current.get("epistemic", "")
        lines: list[str] = ["# 我的世界状态（现实）"]

        window_name = WINDOW_NAME_BY_SLUG.get(current.get("window", ""), "")
        if window_name:
            lines.append(f"- 现在是{window_name}时段")
        if epistemic == "fact":
            if current.get("source") == SOURCE_OBSERVED:
                lines.append(f"- 最近观察到：{current['activity']}")
            else:
                lines.append(f"- 你此刻正在：{current['activity']}")
        elif epistemic == "committed":
            lines.append(f"- 按今天的安排，你此刻应该在：{current['activity']}")
        else:
            lines.append(f"- 按你的作息，此刻你大概在：{current['activity']}（可被你自己的实际状态覆盖）")

        location = current.get("location") or self.location
        if location:
            lines.append(f"- 当前位置：{location}")

        plan_lines = self.today_plan_view(now_ts)
        if plan_lines:
            lines.append("- 今天的安排：")
            lines.extend(plan_lines)

        lines.append(
            "- 以上是你自己的生活状态。聊天时自然带入（例如正在做的事、"
            "忙不忙），但不要把它强加为对方可见的事实；与对方消息冲突时以对方为准。"
        )
        return "\n".join(lines)

    # ── 持久化 ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "wake_minute": self.wake_minute,
            "sleep_minute": self.sleep_minute,
            "location": self.location,
            "location_source": self.location_source,
            "location_item_id": self.location_item_id,
            "location_expires_at": self.location_expires_at,
            "routine": dict(self.routine),
            "plan_items": [item.to_dict() for item in self.plan_items],
            "observations": [item.to_dict() for item in self.observations],
            "last_day_key": self._last_day_key,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DailyLifeState":
        state = cls()
        if not isinstance(data, dict):
            return state
        state.wake_minute = _clamp_minute(data.get("wake_minute"), state.wake_minute)
        state.sleep_minute = _clamp_minute(data.get("sleep_minute"), state.sleep_minute)
        state.location = str(data.get("location", "") or "").strip()[:40]
        state.location_source = str(data.get("location_source", "persistent") or "persistent")[:32]
        state.location_item_id = str(data.get("location_item_id", "") or "")[:32]
        raw_location_expiry = data.get("location_expires_at")
        state.location_expires_at = (
            _safe_float(raw_location_expiry, 0.0)
            if raw_location_expiry is not None else None
        )
        if state.location_expires_at is not None and state.location_expires_at <= 0:
            state.location_expires_at = None
        raw_routine = data.get("routine", {})
        if isinstance(raw_routine, dict):
            state.routine = {
                normalize_window(key): str(value or "").strip()
                for key, value in raw_routine.items()
                if normalize_window(key) and str(value or "").strip()
            }
        raw_plan = data.get("plan_items", [])
        if isinstance(raw_plan, list):
            state.plan_items = [
                item
                for item in (AgendaItem.from_dict(raw) for raw in raw_plan)
                if item is not None
            ][-_MAX_PLAN_ITEMS:]
        raw_obs = data.get("observations", [])
        if isinstance(raw_obs, list):
            state.observations = [
                item
                for item in (AgendaItem.from_dict(raw) for raw in raw_obs)
                if item is not None
            ][-_MAX_OBSERVATIONS:]
        state._last_day_key = str(data.get("last_day_key", "") or "")
        state.revision = max(0, _safe_int(data.get("revision"), 0))
        state.updated_at = _safe_float(data.get("updated_at"), 0.0)
        return state


def _parse_schedule_clock(value: Any) -> int:
    """日程 API 的时间解析：数值分钟允许跨午夜，文本仍严格校验。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes = int(value)
        return minutes % (24 * 60) if minutes >= 0 else -1
    return parse_clock(value)


def _safe_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _end_time_as_timestamp(value: Any, created_at: float) -> float | None:
    """将当日结束时间转换为最近的未来本地时间戳。"""
    end_minute = _parse_schedule_clock(value)
    if end_minute < 0:
        return None
    local = time.localtime(created_at)
    base = time.struct_time((
        local.tm_year, local.tm_mon, local.tm_mday, end_minute // 60,
        end_minute % 60, 0, local.tm_wday, local.tm_yday, local.tm_isdst,
    ))
    stamp = time.mktime(base)
    if stamp <= created_at:
        stamp += 24 * 60 * 60
    return stamp


def _clamp_minute(value: Any, default: int) -> int:
    minute = _safe_int(value, default)
    return minute % (24 * 60) if 0 <= minute < 24 * 60 else default


__all__ = [
    "AgendaItem",
    "DailyLifeState",
    "DEFAULT_ROUTINE",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_CONFIRMED",
    "STATUS_EXPIRED",
    "STATUS_PLANNED",
    "SCHEDULE_WINDOWS",
    "WINDOW_NAME_BY_SLUG",
    "normalize_window",
    "parse_clock",
    "window_for_minutes",
    "window_for_ts",
]
