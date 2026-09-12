# -*- coding: utf-8 -*-
"""新核心机制端到端自检模拟（无 LLM，固定评估结果驱动）。

模拟一段跨越两天的关系生命周期：
  第一天：日常聊天 → 被冷落 → 内驱变化 → 意图生长 → 承诺到期主动
  第二天：入戏 → 剧情演化 → 现实打断（暂停/恢复）→ 出戏存档 → 信念渲染
打印每个阶段的状态快照，验证机制链路真实可用。
"""
import asyncio
import sys
import time
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent          # plugins/neo_fatum_chatter
_PLUGINS_DIR = _PLUGIN_DIR.parent        # plugins
_REPO_ROOT = _PLUGINS_DIR.parent         # 仓库根（提供 src.*）
for _p in (str(_REPO_ROOT), str(_PLUGINS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.domain.world import WorldTracker
from neo_fatum_chatter.services.appraisal import AppraisalResult, apply_appraisal


def banner(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def show(session: NFCSession, label: str) -> None:
    d = session.drives
    state_text = d.render_state_text().replace("# 我的状态\n", "") or "（状态平稳，不渲染）"
    print(f"\n[{label}]")
    print("  内驱: 社交欲{:.2f} 精力{:.2f} 好奇{:.2f} 被忽视{:.2f} 心情{:.2f}".format(
        d.social_drive, d.energy, d.curiosity, d.neglect, d.mood
    ))
    for line in state_text.splitlines():
        print("  " + line)
    pending = [f"{i.kind}:{i.content[:14]}({i.urge:.2f})" for i in session.intents.intents if i.fired_at is None]
    print("  意图: " + ("；".join(pending) if pending else "无"))
    print("  登记: " + session.active_register + (
        f"（剧情: {session.story_world.story.location or '-'}）" if session.story_world else ""
    ))


async def main() -> None:
    config = NFCConfig()
    session = NFCSession(user_id="u1", stream_id="s1")
    base = time.time()

    banner("第一天上午 · 日常聊天")
    session.drives.last_updated = base - 7200  # 已两小时没说话
    apply_appraisal(session, AppraisalResult(emotion="开心", topic_hooks=["她周五要面试"]), config=config)
    session.add_user_message("早呀，我今天好困", user_name="小满", user_id="u1", timestamp=base)
    session.drives.on_bot_reply(2)
    show(session, "聊了一轮之后")
    assert session.drives.social_drive < 0.5, "消息应满足社交欲"

    banner("第一天下午 · 说完话被晾着（等待超时 x3）")
    for i in range(3):
        session.drives.on_wait_timeout(base + 3600 * (i + 1))
    session.drives.on_reply_timing(in_time=False, now=base + 4 * 3600)
    show(session, "三次超时 + 迟到回复")
    assert session.drives.neglect > 0.2 and session.drives.mood < 0.5, "被冷落感上升、心情下滑"

    banner("第一天深夜 · 承诺登记（S1 抽到约定）")
    apply_appraisal(session, AppraisalResult(
        commitment={"content": "面试完听她复盘", "due_hours": 20},
    ), config=config)
    show(session, "承诺入队")

    banner("第二天 · 时间快进 26 小时（意图冲动随时间生长）")
    future = base + 26 * 3600
    session.drives.advance_to(future)
    session.intents.grow_all(future)
    session.intents.prune(future)
    winner = session.intents.peek_fire(config.intent.fire_threshold, now=future)
    show(session, "承诺到期")
    assert winner is not None and winner.kind == "commitment", "到期承诺应冲到阈值"
    print(f"  → 触发主动：{winner.content}（urge={winner.urge:.2f}）")
    session.intents.mark_fired(winner.id, future)
    session.drives.on_proactive_fired(future)

    banner("第二天晚上 · 对方发起剧情（显式入戏）")
    apply_appraisal(session, AppraisalResult(frame_event="enter_story"), config=config)
    apply_appraisal(session, AppraisalResult(
        register="story",
        story_update={"location": "雨夜的便利店", "story_time": "深夜", "present": ["我", "她"]},
        world_facts=["她淋着雨跑进来"],
    ), config=config)
    session.beliefs.upsert("剧情：她怕打雷", subject="story", register="story", confidence=0.8)
    show(session, "剧情进行中")

    banner("现实急事打断（pause → 处理现实 → resume）")
    apply_appraisal(session, AppraisalResult(frame_event="pause_story"), config=config)
    show(session, "剧情暂停")
    assert session.active_register == "reality" and session.story_world is not None
    apply_appraisal(session, AppraisalResult(frame_event="resume_story"), config=config)
    assert session.active_register == "story"

    banner("剧情收尾（exit → 存档）")
    apply_appraisal(session, AppraisalResult(frame_event="exit_story"), config=config)
    show(session, "出戏")
    assert session.story_archive, "有意义的故事应被存档"
    print(f"  → 已存档故事：{session.story_archive[0].title}")

    banner("持久化往返 + 上下文渲染")
    restored = NFCSession.from_dict(session.to_dict())
    assert restored.drives.to_dict() == session.drives.to_dict()
    assert restored.story_archive[0].title == session.story_archive[0].title
    from neo_fatum_chatter.context.sources.state_source import build_state_contributions
    contributions = build_state_contributions(restored, config)
    print(f"  状态贡献 {len(contributions)} 块: " + ", ".join(c.source for c in contributions))
    for c in contributions:
        print("  ──", c.source)
        for line in c.content.splitlines()[:4]:
            print("     " + line)

    banner("语义化打字延迟样例")
    from neo_fatum_chatter.execution.reply_executor import compute_segment_delay
    samples = [
        ("嗯。", "好"),
        ("你吃饭了吗？", "刚吃完，你呢"),
        ("其实我……", "怎么了？跟我说说"),
        ("哈哈", "我跟你说今天遇到个超好笑的事你必须听"),
    ]
    for prev, nxt in samples:
        delay = compute_segment_delay(prev, nxt, chars_per_sec=15.0, delay_min=0.5, delay_max=4.0)
        print(f"  {prev!r} → {nxt!r:24s} 延迟 {delay:.2f}s")

    print("\n全部断言通过：机制链路（内驱→评估→意图→主动/剧情→存档→渲染）端到端可用 ✓")


if __name__ == "__main__":
    asyncio.run(main())
