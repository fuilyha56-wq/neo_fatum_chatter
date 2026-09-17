"""NFC S1 感知评估服务（System 1）。

每批新消息在进入主模型决策前，先用轻量模型（sub_actor）产出一次
结构化"第一反应"：情绪、紧急度、登记簿归属、场景/剧情事实、
话题钩子、承诺、换挡事件、自然延迟建议、隐藏事实揭示判断。

设计原则：
    - **顾问而非闸门**：任何失败（超时/解析错误/模型缺失）都返回 None，
      主流程退回旧行为，S1 永不阻塞对话。
    - **显式标记优先于风格推断**：换挡事件（入戏/出戏）主要由模型判定的
      显式信号触发；隐式 register 分类带粘滞（连续两次一致才换挡），
      单条模糊消息不切换登记簿。
    - 评估产物全部落到显式状态数据上（drives/world/intents/角色卡），
      判定错了下一轮可改判。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from ..domain.world import REGISTER_STORY, WorldTracker, is_frame_event

logger = get_logger("NFC_appraisal")

# 情绪极性关键词（用于内驱 mood 微调，避免再花一次 LLM）
_POSITIVE_WORDS = ("开心", "高兴", "喜欢", "兴奋", "暖心", "甜", "笑", "惊喜", "感动")
_NEGATIVE_WORDS = ("难过", "生气", "烦", "累", "委屈", "失望", "难过", "冷", "吵", "哭")


@dataclass
class AppraisalResult:
    """S1 评估的结构化产物。"""

    emotion: str = ""
    urgency: float = 0.3
    register: str = ""           # "reality" | "story" | ""（不确定）
    defer_seconds: float = 0.0
    world_facts: list[str] = field(default_factory=list)
    story_update: dict[str, Any] | None = None
    topic_hooks: list[str] = field(default_factory=list)
    commitment: dict[str, Any] | None = None
    frame_event: str = ""        # enter_story|pause_story|resume_story|exit_story|""
    respond_now: bool = True
    reveal_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


_APPRAISAL_SYSTEM_PROMPT = """\
你是一个即时评估器。你为角色扮演 AI 的"第一反应"服务：角色收到一批新消息后、\
正式组织回复前，你先快速评估这批消息。只输出 JSON，不要输出任何其他内容。

输入会告诉你：当前所处的世界（reality=现实聊天 / story=剧情演绎）、\
角色的隐藏事实揭示条件、当前内驱状态。你评估的对象是最新的 [新消息]。

输出 JSON 字段：
{
  "emotion": "角色此刻的第一情绪反应，一两个词，例如：有点担心/开心/无语",
  "urgency": 0.0~1.0，消息需要多快被回应（急事=1.0，闲聊=0.2）,
  "register": "reality" 或 "story" 或 ""（无法判断时留空）。
      reality=现实世界的日常聊天；story=对方在发起/延续剧情演绎
      （想象场景、扮演、旁白动作、星号动作描写等叙事性内容），
      与当前世界一致时照抄当前世界,
  "frame_event": "" | "enter_story" | "pause_story" | "resume_story" | "exit_story"。
      仅在出现明确换挡信号时输出：
      enter_story=对方明确提出开始一段想象/扮演/剧情且当前不在剧情中；
      pause_story=剧情进行中对方明确转向现实急事；
      resume_story=对方明确要求回到之前暂停的剧情；
      exit_story=对方明确结束剧情（"不想玩了"、"回到现实吧"、剧情自然收尾）。
      没有明确信号一律留空,
  "world_facts": ["从消息中确认的现实场景事实，最多3条，没有就空数组"],
  "story_update": {"story_time":"故事内时间","location":"地点","present":["在场角色"],"ongoing_event":"正在发生的事"}
      仅 register=story 时填写，没有新信息则 null,
  "topic_hooks": ["值得以后追问的话题钩子，最多2条。例如'她提到周五要面试'。没有则空数组"],
  "commitment": {"content":"对方或角色作出的承诺内容","due_hours":24} 或 null。
      仅捕捉明确的约定（'明天发给你'、'周末一起'），不要过度推断,
  "defer_seconds": 0~20，建议角色"看到消息后过多久再回"。
      急事=0；普通闲聊=2~8；对方在连发牢骚/讲故事=8~15（等对方说完）,
  "respond_now": true/false，false 表示这批消息不需要回复（纯表情、无需回应的表情包等）,
  "reveal_ids": ["满足揭示条件的隐藏事实 id 列表，没有则空数组"]
}

要求：宁可保守不要过度推断；不确定的字段用空值。只输出 JSON。"""


def build_appraisal_user_prompt(
    messages_text: str,
    *,
    session: Any,
    config: Any,
) -> str:
    """组装 S1 评估的 user prompt（紧凑，控制 token）。"""
    active_register = getattr(session, "active_register", "reality")
    parts = [
        f"当前世界：{active_register}",
    ]

    drives = getattr(session, "drives", None)
    if drives is not None:
        state_text = drives.render_state_text()
        parts.append(
            state_text.replace("# 我的状态", "角色当前内驱状态：").replace("- ", "")
            if state_text
            else "角色当前内驱状态：平稳"
        )

    from ..domain.character_card import CharacterCard

    card = CharacterCard.from_config(getattr(config, "character", None))
    card.merge_state(getattr(session, "character_state", None))
    conditions = card.hidden_condition_brief()
    if conditions:
        parts.append(f"隐藏事实（未揭示）：\n{conditions}")

    story_world = getattr(session, "story_world", None)
    if active_register == REGISTER_STORY and story_world is not None:
        facts = story_world.story
        brief = "、".join(
            x
            for x in (
                f"时间={facts.story_time}" if facts.story_time else "",
                f"地点={facts.location}" if facts.location else "",
                f"在场={'/'.join(facts.present)}" if facts.present else "",
                f"事件={facts.ongoing_event}" if facts.ongoing_event else "",
            )
            if x
        )
        if brief:
            parts.append(f"当前剧情：{brief}")

    # 截断消息文本，S1 只需要看最近的内容
    trimmed = messages_text[-1500:]
    parts.append(f"[新消息]\n{trimmed}")

    return "\n\n".join(parts)


def parse_appraisal(raw_text: str) -> AppraisalResult | None:
    """宽松解析 S1 的 JSON 输出。失败返回 None。"""
    text = (raw_text or "").strip()
    if not text:
        return None
    # 剥可能的最外层 markdown 围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    result = AppraisalResult(raw=data)
    result.emotion = str(data.get("emotion", "") or "")[:40]
    try:
        result.urgency = max(0.0, min(1.0, float(data.get("urgency", 0.3))))
    except (TypeError, ValueError):
        result.urgency = 0.3

    register = str(data.get("register", "") or "").lower()
    result.register = register if register in {"reality", "story"} else ""

    frame_event = str(data.get("frame_event", "") or "").strip()
    result.frame_event = frame_event if is_frame_event(frame_event) else ""

    raw_facts = data.get("world_facts", [])
    if isinstance(raw_facts, list):
        result.world_facts = [
            str(f).strip() for f in raw_facts if str(f).strip()
        ][:3]

    story_update = data.get("story_update")
    if isinstance(story_update, dict) and any(
        str(v).strip() for v in story_update.values() if isinstance(v, (str, int, float))
    ):
        result.story_update = story_update

    raw_hooks = data.get("topic_hooks", [])
    if isinstance(raw_hooks, list):
        result.topic_hooks = [
            str(h).strip() for h in raw_hooks if str(h).strip()
        ][:2]

    commitment = data.get("commitment")
    if isinstance(commitment, dict) and str(commitment.get("content", "")).strip():
        result.commitment = {
            "content": str(commitment.get("content", "")).strip()[:120],
            "due_hours": _as_positive_float(commitment.get("due_hours"), 24.0),
        }

    try:
        result.defer_seconds = max(
            0.0, min(20.0, float(data.get("defer_seconds", 0) or 0))
        )
    except (TypeError, ValueError):
        result.defer_seconds = 0.0

    result.respond_now = bool(data.get("respond_now", True))

    raw_reveals = data.get("reveal_ids", [])
    if isinstance(raw_reveals, list):
        result.reveal_ids = [str(r).strip() for r in raw_reveals if str(r).strip()][:3]

    return result


async def appraise_messages(
    messages_text: str,
    *,
    session: Any,
    config: Any,
    raw_input_chars: int | None = None,
) -> AppraisalResult | None:
    """对一批新消息执行 S1 评估。失败/跳过一律返回 None。

    Args:
        messages_text: 格式化后的消息文本（含时间戳/发送者包装）。
        session: 当前会话状态。
        config: NFC 配置。
        raw_input_chars: 消息本体的原始字符数。格式化文本的包装
            （时间戳/昵称/QQ号）会让 ``min_input_chars`` 永远不触发，
            因此建议调用方传入原始长度；缺省时退回用格式化文本计量。
    """
    appraisal_cfg = getattr(config, "appraisal", None)
    if appraisal_cfg is None or not appraisal_cfg.enabled:
        return None

    text = (messages_text or "").strip()
    probe_len = (
        len(text) if raw_input_chars is None else max(0, int(raw_input_chars))
    )
    if probe_len < int(getattr(appraisal_cfg, "min_input_chars", 8)):
        return None

    try:
        model_set = get_model_set_by_task(appraisal_cfg.model_task)
    except Exception:
        model_set = None
    if not model_set:
        logger.debug("[S1] 评估模型不可用，跳过")
        return None

    request = create_llm_request(model_set, "NFC_appraisal")
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_APPRAISAL_SYSTEM_PROMPT)))
    request.add_payload(
        LLMPayload(
            ROLE.USER,
            Text(build_appraisal_user_prompt(text, session=session, config=config)),
        )
    )

    try:
        llm_response = await asyncio.wait_for(
            request.send(),
            timeout=float(appraisal_cfg.timeout_seconds),
        )
        raw_text = (await llm_response or "").strip()
    except asyncio.TimeoutError:
        logger.debug("[S1] 评估超时，本轮跳过")
        return None
    except Exception as exc:
        logger.debug(f"[S1] 评估调用失败，本轮跳过: {exc}")
        return None

    result = parse_appraisal(raw_text)
    if result is not None:
        logger.info(
            f"[S1] emotion={result.emotion!r} register={result.register or '-'} "
            f"frame={result.frame_event or '-'} defer={result.defer_seconds:.1f}s "
            f"hooks={len(result.topic_hooks)} facts={len(result.world_facts)}"
        )
    return result


def harvest_pending_appraisal(
    session: Any,
    config: Any,
    *,
    cancel_if_running: bool = False,
) -> None:
    """收割已完成的并行 S1 评估任务（非阻塞，副决策语义）。

    - 任务已完成：应用其结果（状态写入 + "缓一缓"意见落到
      ``respond_not_before``，由下一次决策请求采用）。
    - 任务未完成：``cancel_if_running=True`` 时取消（批次已过期），
      否则保留在槽位，留给下一轮收割——绝不等待、绝不干扰主模型。
    """
    task = getattr(session, "_s1_task", None)
    if task is None:
        return
    session._s1_task = None
    if not task.done():
        if cancel_if_running:
            task.cancel()
            logger.debug("[S1] 未完成的过期评估已丢弃")
        else:
            session._s1_task = task
        return
    try:
        appraisal = task.result()
    except Exception:
        logger.debug("[S1] 并行评估任务失败，丢弃")
        return
    if appraisal is None:
        return
    note = apply_appraisal(session, appraisal, config=config)
    if note != "no-op":
        logger.info(f"[S1] 评估已应用: {note}")


def apply_appraisal(session: Any, appraisal: AppraisalResult, *, config: Any) -> str:
    """把 S1 评估产物落到 session 的显式状态上。返回应用摘要（日志用）。"""
    WorldTracker.ensure_initialized(session)
    now = time.time()
    applied: list[str] = []

    # 1. 换挡事件（显式信号优先）
    if appraisal.frame_event:
        note = WorldTracker.apply_frame_event(session, appraisal.frame_event)
        applied.append(f"frame:{appraisal.frame_event}({note})")
        session._register_mismatch_count = 0
        if appraisal.frame_event == "enter_story":
            # 剧情开始时把覆层提示交给决策层自然形成，这里不强行写卡
            pass
    elif appraisal.register and appraisal.register != session.active_register:
        # 隐式分类：粘滞——连续两次一致才换挡
        session._register_mismatch_count += 1
        if session._register_mismatch_count >= 2:
            event = (
                "enter_story" if appraisal.register == REGISTER_STORY else "pause_story"
            )
            note = WorldTracker.apply_frame_event(session, event)
            applied.append(f"implicit:{event}({note})")
            session._register_mismatch_count = 0
    else:
        session._register_mismatch_count = 0

    # 2. 世界事实：按激活登记簿落位
    active = session.active_register
    if appraisal.world_facts:
        if active == REGISTER_STORY and session.story_world is not None:
            for fact in appraisal.world_facts:
                session.story_world.add_evidence(
                    "appraisal", fact, "user_message", 0.7
                )
        else:
            from ..domain.scene_state import SceneEvidence

            for fact in appraisal.world_facts:
                if any(e.content == fact for e in session.scene_state.evidence):
                    continue
                session.scene_state.evidence.append(
                    SceneEvidence(
                        source="appraisal", content=fact, kind="user_message", confidence=0.7
                    )
                )
                if len(session.scene_state.evidence) > 20:
                    session.scene_state.evidence = session.scene_state.evidence[-20:]
                session.scene_state.revision += 1
                session.scene_state.updated_at = now
            if session.scene_state.certainty == "unknown":
                session.scene_state.certainty = "weak"
                session.scene_state.revision += 1
                session.scene_state.updated_at = now
            applied.append(f"facts:{len(appraisal.world_facts)}")

    # 3. 剧情事实更新
    if (
        appraisal.story_update
        and active == REGISTER_STORY
        and session.story_world is not None
    ):
        if session.story_world.story.update(appraisal.story_update):
            session.story_world.revision += 1
            session.story_world.updated_at = now
            applied.append("story_update")

    # 4. 话题钩子与承诺 → 意图队列
    for hook in appraisal.topic_hooks:
        session.intents.add(hook, kind="topic_hook", register=active, urge=0.35)
    if appraisal.topic_hooks:
        applied.append(f"hooks:{len(appraisal.topic_hooks)}")
    if appraisal.commitment:
        due_hours = float(appraisal.commitment.get("due_hours", 24.0) or 24.0)
        session.intents.add(
            appraisal.commitment.get("content", ""),
            kind="commitment",
            register=active,
            urge=0.5,
            deadline=now + max(0.5, due_hours) * 3600.0,
        )
        applied.append("commitment")

    # 5. 隐藏事实揭示
    if appraisal.reveal_ids:
        from ..domain.character_card import CharacterCard

        card = CharacterCard.from_config(getattr(config, "character", None))
        card.merge_state(getattr(session, "character_state", None))
        newly = card.reveal(appraisal.reveal_ids)
        if newly:
            session.character_state = card.export_state()
            applied.append(f"reveal:{len(newly)}")

    # 6. 情绪 → 内驱微调（关键词极性，不花额外 LLM）
    if appraisal.emotion:
        polarity = _emotion_polarity(appraisal.emotion)
        if polarity != 0:
            session.drives.mood = max(
                0.0, min(1.0, session.drives.mood + 0.06 * polarity)
            )

    # 7. 自然延迟
    defer_cfg_enabled = bool(getattr(getattr(config, "appraisal", None), "defer_enabled", False))
    if defer_cfg_enabled and appraisal.defer_seconds >= 0.5 and not session.is_waiting():
        max_defer = float(getattr(getattr(config, "appraisal", None), "max_defer_seconds", 18.0))
        session.respond_not_before = now + min(appraisal.defer_seconds, max_defer)
        applied.append(f"defer:{appraisal.defer_seconds:.0f}s")
    else:
        session.respond_not_before = 0.0

    return "; ".join(applied) if applied else "no-op"


def _emotion_polarity(emotion: str) -> int:
    text = emotion or ""
    if any(w in text for w in _POSITIVE_WORDS):
        return 1
    if any(w in text for w in _NEGATIVE_WORDS):
        return -1
    return 0


def _as_positive_float(value: Any, default: float) -> float:
    try:
        result = float(value)
        return result if result > 0 else default
    except (TypeError, ValueError):
        return default
