"""NFC 双登记簿世界状态（Dual-Register World State）。

把"我们在哪个世界"做成数据：现实登记簿（reality）沿用防幻觉的
SceneState 语义；故事登记簿（story）是被授权虚构的剧情世界状态机。
两种玩法（拟人聊天 / 剧情 RP）共用同一引擎，只是激活的登记簿不同。

换挡四件套由 :class:`WorldTracker` 承担：
    - enter_story   进入剧情（无则新建故事世界，有则视作 resume）
    - pause_story   现实急事打断，故事世界原样保留
    - resume_story  回到暂停的故事
    - exit_story    结束剧情：有意义的故事世界存档后摘除

故事存档（StoryArchiveEntry）= 世界状态 + 覆层卡 + 剧情梗概，
支持"继续昨天那个故事"的多档恢复。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .scene_state import SceneEvidence, SceneState

REGISTER_REALITY = "reality"
REGISTER_STORY = "story"

_FRAME_EVENTS = frozenset(
    {"enter_story", "pause_story", "resume_story", "exit_story"}
)

_MAX_STORY_EVIDENCE = 30
_MAX_STORY_ARCHIVE = 5


def is_frame_event(name: str) -> bool:
    return name in _FRAME_EVENTS


@dataclass
class StoryFacts:
    """故事登记簿独有的剧情事实。"""

    story_time: str = ""      # 故事内时间，如 "第一夜" / "傍晚"
    location: str = ""        # 故事内地点
    present: list[str] = field(default_factory=list)  # 在场角色
    ongoing_event: str = ""   # 正在发生的事件

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_time": self.story_time,
            "location": self.location,
            "present": list(self.present),
            "ongoing_event": self.ongoing_event,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> StoryFacts:
        facts = cls()
        if not isinstance(data, dict):
            return facts
        facts.story_time = str(data.get("story_time", "") or "")
        facts.location = str(data.get("location", "") or "")
        raw_present = data.get("present", [])
        if isinstance(raw_present, list):
            facts.present = [str(p) for p in raw_present if str(p).strip()]
        facts.ongoing_event = str(data.get("ongoing_event", "") or "")
        return facts

    def update(self, patch: dict[str, Any] | None) -> bool:
        """用 S1 评估给出的剧情补丁更新事实，返回是否有变化。"""
        if not isinstance(patch, dict):
            return False
        changed = False
        for key in ("story_time", "location", "ongoing_event"):
            value = patch.get(key)
            if isinstance(value, str) and value.strip() and value.strip() != getattr(self, key):
                setattr(self, key, value.strip())
                changed = True
        raw_present = patch.get("present")
        if isinstance(raw_present, list) and raw_present:
            merged = list(self.present)
            for item in raw_present:
                name = str(item).strip()
                if name and name not in merged:
                    merged.append(name)
                    changed = True
            self.present = merged[-10:]
        return changed

    def is_empty(self) -> bool:
        return not (
            self.story_time or self.location or self.present or self.ongoing_event
        )


@dataclass
class WorldState:
    """单个登记簿的世界状态。

    reality 实例复用 SceneState 的防幻觉语义（certainty/evidence）；
    story 实例额外携带 StoryFacts，certainty 默认弱化为可叙事状态。
    """

    register: str = REGISTER_REALITY
    certainty: str = "unknown"
    social_channel: str = ""
    location_type: str = "unknown"
    device_assumption_allowed: bool = False
    evidence: list[SceneEvidence] = field(default_factory=list)
    story: StoryFacts = field(default_factory=StoryFacts)
    updated_at: float = 0.0

    @classmethod
    def new_story(cls) -> WorldState:
        state = cls(register=REGISTER_STORY)
        state.certainty = "confirmed"  # 剧情世界是被授权虚构的
        state.device_assumption_allowed = True
        state.updated_at = time.time()
        return state

    @classmethod
    def from_scene_state(cls, scene: SceneState) -> WorldState:
        """从旧版 SceneState 迁移为现实登记簿。"""
        return cls(
            register=REGISTER_REALITY,
            certainty=scene.certainty,
            social_channel=scene.social_channel,
            location_type=scene.location_type,
            device_assumption_allowed=scene.device_assumption_allowed,
            evidence=list(scene.evidence),
            updated_at=time.time(),
        )

    def add_evidence(self, source: str, content: str, kind: str, confidence: float) -> bool:
        text = (content or "").strip()
        if not text:
            return False
        # 同内容去重：重复出现视作强化（抬置信度）
        for item in self.evidence:
            if item.content == text:
                item.confidence = min(1.0, max(item.confidence, confidence))
                self.updated_at = time.time()
                return False
        self.evidence.append(
            SceneEvidence(
                source=source,
                content=text,
                kind=kind,
                confidence=max(0.0, min(1.0, confidence)),
            )
        )
        if len(self.evidence) > _MAX_STORY_EVIDENCE:
            self.evidence = self.evidence[-_MAX_STORY_EVIDENCE:]
        self.updated_at = time.time()
        return True

    def has_story_content(self) -> bool:
        return self.register == REGISTER_STORY and (
            not self.story.is_empty() or bool(self.evidence)
        )

    def render_story_text(self, character_name: str = "") -> str:
        """渲染故事世界状态块（仅 story 登记簿有意义）。"""
        if self.register != REGISTER_STORY:
            return ""
        lines: list[str] = ["# 剧情世界（你们正在共同演绎的故事）"]
        facts = self.story
        if facts.story_time:
            lines.append(f"- 故事时间：{facts.story_time}")
        if facts.location:
            lines.append(f"- 当前地点：{facts.location}")
        if facts.present:
            lines.append(f"- 在场角色：{'、'.join(facts.present)}")
        if facts.ongoing_event:
            lines.append(f"- 正在发生：{facts.ongoing_event}")
        for item in self.evidence[-10:]:
            if item.content:
                lines.append(f"- 剧情事实：{item.content}")
        lines.append(
            "- 以上是已确立的剧情设定。在故事里你可以自然地叙事和描写，"
            "但不要跳出故事引用现实世界的系统信息。"
        )
        _ = character_name
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "register": self.register,
            "certainty": self.certainty,
            "social_channel": self.social_channel,
            "location_type": self.location_type,
            "device_assumption_allowed": self.device_assumption_allowed,
            "evidence": [
                {
                    "source": e.source,
                    "content": e.content,
                    "kind": e.kind,
                    "confidence": e.confidence,
                }
                for e in self.evidence
            ],
            "story": self.story.to_dict(),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> WorldState:
        if not isinstance(data, dict):
            return cls()
        state = cls()
        register = str(data.get("register", REGISTER_REALITY) or REGISTER_REALITY)
        state.register = (
            REGISTER_STORY if register == REGISTER_STORY else REGISTER_REALITY
        )
        state.certainty = str(data.get("certainty", "unknown") or "unknown")
        state.social_channel = str(data.get("social_channel", "") or "")
        state.location_type = str(data.get("location_type", "unknown") or "unknown")
        state.device_assumption_allowed = bool(
            data.get("device_assumption_allowed", False)
        )
        raw_evidence = data.get("evidence", [])
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "") or "").strip()
                if not content:
                    continue
                state.evidence.append(
                    SceneEvidence(
                        source=str(item.get("source", "appraisal") or "appraisal"),
                        content=content,
                        kind=str(item.get("kind", "inference") or "inference"),
                        confidence=_as_float(item.get("confidence"), 0.5),
                    )
                )
        state.story = StoryFacts.from_dict(data.get("story"))
        state.updated_at = _as_float(data.get("updated_at"), 0.0)
        return state


@dataclass
class StoryArchiveEntry:
    """一个已存档的故事（可恢复续写）。"""

    id: str
    title: str
    saved_at: float
    world: dict[str, Any]
    overlay_name: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "saved_at": self.saved_at,
            "world": self.world,
            "overlay_name": self.overlay_name,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> StoryArchiveEntry | None:
        if not isinstance(data, dict):
            return None
        entry = cls(
            id=str(data.get("id", "") or uuid.uuid4().hex[:8]),
            title=str(data.get("title", "未命名故事") or "未命名故事"),
            saved_at=_as_float(data.get("saved_at"), time.time()),
            world=data.get("world") if isinstance(data.get("world"), dict) else {},
            overlay_name=str(data.get("overlay_name", "") or ""),
            summary=str(data.get("summary", "") or ""),
        )
        return entry


class WorldTracker:
    """session 世界字段的管理器：换挡、迁移、归档。

    session 上的字段约定：
        - ``scene_state``: 现实登记簿（沿用旧字段，兼容已持久化会话）
        - ``story_world``: 活跃/暂停的故事世界（WorldState|None）
        - ``active_register``: "reality" | "story"
        - ``story_archive``: list[StoryArchiveEntry]
    """

    @staticmethod
    def apply_frame_event(session: Any, event: str, *, story_summary: str = "") -> str:
        """对 session 应用换挡事件，返回人类可读的结果描述。"""
        if event == "enter_story":
            if getattr(session, "story_world", None) is not None:
                session.active_register = REGISTER_STORY
                return "剧情恢复（沿用暂停中的故事世界）"
            session.story_world = WorldState.new_story()
            session.active_register = REGISTER_STORY
            return "进入剧情（新建故事世界）"

        if event == "resume_story":
            if getattr(session, "story_world", None) is None:
                return "无可恢复的故事"
            session.active_register = REGISTER_STORY
            return "剧情恢复"

        if event == "pause_story":
            session.active_register = REGISTER_REALITY
            return "剧情暂停（世界保留，随时可回来）"

        if event == "exit_story":
            story = getattr(session, "story_world", None)
            # 覆层状态持久化在 character_state["active_overlay"]（见 CharacterCard.export_state）
            char_state = getattr(session, "character_state", None)
            overlay = (
                char_state.get("active_overlay")
                if isinstance(char_state, dict)
                else None
            )
            if story is not None and story.has_story_content():
                archive = getattr(session, "story_archive", None)
                if archive is None:
                    archive = []
                    session.story_archive = archive
                title = (
                    story.story.ongoing_event
                    or story.story.location
                    or f"故事 {time.strftime('%m-%d', time.localtime())}"
                )
                archive.append(
                    StoryArchiveEntry(
                        id=uuid.uuid4().hex[:8],
                        title=title[:40],
                        saved_at=time.time(),
                        world=story.to_dict(),
                        overlay_name=str(
                            overlay.get("name", "")
                            if isinstance(overlay, dict)
                            else ""
                        ),
                        summary=story_summary[:500],
                    )
                )
                session.story_archive = archive[-_MAX_STORY_ARCHIVE:]
            session.story_world = None
            session.active_register = REGISTER_REALITY
            # 出戏摘覆层：渲染层 merge_state 遇到非 dict 的 active_overlay 会忽略
            if isinstance(char_state, dict) and overlay is not None:
                char_state["active_overlay"] = None
            return "剧情结束（已存档）"

        return f"未知换挡事件: {event}"

    @staticmethod
    def restore_archive(session: Any, archive_id: str) -> str:
        """从存档恢复一个故事作为活跃故事世界。"""
        archive = getattr(session, "story_archive", None) or []
        for entry in archive:
            if not isinstance(entry, StoryArchiveEntry):
                continue
            if entry.id == archive_id:
                session.story_world = WorldState.from_dict(entry.world)
                session.story_world.updated_at = time.time()
                session.active_register = REGISTER_STORY
                return f"已恢复故事「{entry.title}」"
        return "未找到该故事存档"

    @staticmethod
    def active_world(session: Any) -> WorldState:
        """取当前激活登记簿对应的世界状态视图。"""
        if (
            getattr(session, "active_register", REGISTER_REALITY) == REGISTER_STORY
            and getattr(session, "story_world", None) is not None
        ):
            return session.story_world
        scene = getattr(session, "scene_state", None)
        if scene is None:
            from .scene_state import SceneState as _S

            scene = _S()
        return WorldState.from_scene_state(scene)

    @staticmethod
    def ensure_initialized(session: Any) -> None:
        """确保 session 带齐世界字段（旧会话迁移）。"""
        if not hasattr(session, "active_register") or not session.active_register:
            session.active_register = REGISTER_REALITY
        if not hasattr(session, "story_world"):
            session.story_world = None
        if not hasattr(session, "story_archive"):
            session.story_archive = []


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default
