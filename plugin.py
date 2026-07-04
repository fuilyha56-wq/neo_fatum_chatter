"""NeoFatumChatter 插件入口。

注册插件、加载配置、注册提示词模板、初始化 Scheduler 任务。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager

from .actions.do_nothing import DoNothingAction
from .actions.query_activity_pattern import QueryActivityPatternAction
from .actions.query_habits import QueryHabitsAction
from .actions.record_habit import RecordHabitAction
from .actions.reply import NFCReplyAction
from .actions.schedule_proactive import ScheduleProactiveAction
from .chatter import NeoFatumChatter
from .config import NFCConfig
from .handlers.proactive_handler import ProactiveHandler
from .handlers.voice_call_history_handler import VoiceCallHistoryHandler
from .session import NFCSessionStore

if TYPE_CHECKING:
    from src.kernel.scheduler import UnifiedScheduler

logger = get_logger("NFC_plugin")


@register_plugin
class NFCPlugin(BasePlugin):
    """NeoFatumChatter 插件。"""

    plugin_name = "neo_fatum_chatter"
    plugin_version = "2.5.4"
    plugin_author = "Lycoris"
    plugin_description = "心理活动流聊天器，模拟真实人类的连续心理活动和对话节奏"
    configs = [NFCConfig]

    _session_store: NFCSessionStore

    def __init__(self, config: NFCConfig | None = None) -> None:
        super().__init__(config)
        max_log_entries = config.prompt.max_log_entries if config else 50
        self._session_store = NFCSessionStore(max_log_entries=max_log_entries)

    @property
    def session_store(self) -> NFCSessionStore:
        """公共访问点，供 Action 和 EventHandler 获取 session store。"""
        return self._session_store

    async def on_plugin_loaded(self) -> None:
        """插件加载时注册提示词模板。调度任务延迟到调度器启动后注册。"""
        # 注册提示词模板
        from .prompts.modules import register_nfc_prompts

        register_nfc_prompts()
        logger.info("NFC 提示词模板已注册")

        # 将配置中的 schedule_proactive 指导语写入 Action 类变量
        config = self.config
        if isinstance(config, NFCConfig) and config.proactive.schedule_guidance:
            ScheduleProactiveAction._guidance = config.proactive.schedule_guidance

        # 预注册已知聊天流的 VLM 跳过（原生多模态模式）
        if isinstance(config, NFCConfig) and config.general.native_multimodal:
            await self._preload_vlm_skip()

        # 延迟注册调度器任务：等待调度器启动
        get_task_manager().create_task(
            self._delayed_scheduler_register(),
            name="NFC_scheduler_init",
            daemon=True,
        )

        # 延迟执行对话中断恢复检查
        get_task_manager().create_task(
            self._check_interrupted_sessions(),
            name="NFC_session_recovery",
            daemon=True,
        )

        logger.info("NFC 插件已加载")

    async def _delayed_scheduler_register(self) -> None:
        """延迟注册调度器任务，等待调度器启动（指数退避，最多 30 秒）。"""
        import asyncio

        from src.kernel.scheduler import get_unified_scheduler

        delay = 0.1  # 初始等待 100ms
        total_waited = 0.0
        max_wait = 30.0

        while total_waited < max_wait:
            await asyncio.sleep(delay)
            total_waited += delay
            try:
                scheduler = get_unified_scheduler()
                if getattr(scheduler, "_running", False):
                    await self._register_scheduler_tasks()
                    return
            except Exception as e:
                logger.debug(f"等待调度器启动时获取 Scheduler 失败: {e}")
            delay = min(delay * 2, 2.0)  # 指数退避，最大 2 秒

        logger.warning("等待调度器启动超时(30s)，放弃注册后台任务")

    async def _preload_vlm_skip(self) -> None:
        """从持久化存储预加载已知聊天流，注册 VLM 跳过。

        在 native_multimodal 模式下，NFC 自行处理图片数据，
        无需框架的 VLM 管线对图片进行文本转述。此方法在插件加载时
        将所有已知会话的 stream_id 注册到 MediaManager 的跳过列表，
        确保重启后已有会话的消息不再触发冗余 VLM 调用。

        注意：首次对话的新用户无法预注册，其第一条消息仍会经过 VLM。
        但由于 base64 数据始终保留在 Message.media 中，NFC 仍能正常
        提取原始图片，不影响功能正确性。

        框架兼容性：若 ``MediaManager`` 不暴露 ``skip_vlm_for_stream``，
        会被降级为 no-op，原生多模态仍可工作（仅多走一次 VLM 文本转述）。
        """
        try:
            stream_ids = await self._session_store.list_all_stream_ids()
            if not stream_ids:
                logger.debug("无历史会话需要预注册 VLM 跳过")
                return

            from src.core.managers.media_manager import get_media_manager

            media_manager = get_media_manager()
            skip_fn = getattr(media_manager, "skip_vlm_for_stream", None)
            if not callable(skip_fn):
                logger.info(
                    "MediaManager 不支持 skip_vlm_for_stream，"
                    "原生多模态降级为兼容模式"
                )
                return

            for stream_id in stream_ids:
                try:
                    skip_fn(stream_id)
                except Exception as exc:
                    logger.debug(
                        f"预注册 VLM 跳过失败 stream={stream_id[:8]}: {exc}"
                    )

            logger.info(
                f"已预注册 {len(stream_ids)} 个聊天流的 VLM 跳过"
            )
        except Exception as e:
            logger.warning(f"预加载 VLM 跳过失败（不影响功能）: {e}")

    async def _register_scheduler_tasks(self) -> None:
        """注册后台调度任务。"""
        config = self.config
        if not isinstance(config, NFCConfig):
            return

        try:
            from src.kernel.scheduler import get_unified_scheduler
            from src.kernel.scheduler.types import TriggerType

            scheduler: UnifiedScheduler = get_unified_scheduler()
        except Exception as e:
            logger.warning(f"获取 Scheduler 失败: {e}")
            return

        # 主动发起检查
        if config.proactive.enabled:
            from .thinker.proactive import ProactiveThinker

            proactive = ProactiveThinker(
                config=config,
                session_store=self._session_store,
            )

            async def proactive_check() -> None:
                """定期检查是否需要主动发起。"""
                triggered = await proactive.check_all_sessions()
                for stream_id in triggered:
                    scheduled_reason = await proactive.mark_triggered(stream_id)
                    logger.info(f"主动发起触发: {stream_id[:8]}")
                    # 通过事件 API 触发 chatter
                    from src.app.plugin_system.api.event_api import publish_event

                    await publish_event(
                        "NFC.proactive_trigger",
                        {"stream_id": stream_id, "scheduled_reason": scheduled_reason},
                    )

            # 注册周期性主动发起检查任务
            await scheduler.create_schedule(
                callback=proactive_check,
                trigger_type=TriggerType.TIME,
                trigger_config={"delay_seconds": config.proactive.check_interval},
                is_recurring=True,
                task_name="NFC_proactive_check",
                force_overwrite=True,
            )

        logger.info("NFC 调度器任务注册完成")

    async def _check_interrupted_sessions(self) -> None:
        """进程重启后检查是否有中断的对话需要恢复。

        检查所有已知 session，如果 DB 中存在比 session.last_activity_at 更新的消息，
        说明重启期间有消息到达但未被处理。通过事件总线触发恢复。
        """
        import asyncio
        import time as _time

        from src.app.plugin_system.api.stream_api import get_stream_messages
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        config = self.config
        if not isinstance(config, NFCConfig) or not config.general.enabled:
            return

        # 等待 stream loop manager 就绪，带指数退避重试。
        # 取代此前固定的 sleep(5.0)：系统启动较慢时，固定等待可能在 slm 尚未
        # 运行时就放弃恢复，导致重启期间到达的消息全部漏处理。退避重试可在
        # 系统就绪后尽快恢复，又设上限避免无限等待。
        slm = get_stream_loop_manager()
        delay = 1.0
        max_total_wait = 60.0
        waited = 0.0
        while not slm.is_running and waited < max_total_wait:
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 10.0)
        if not slm.is_running:
            logger.warning(
                "[NFC] 对话恢复：stream loop manager 启动超时，跳过本次恢复检查"
            )
            return

        session_store = self._session_store
        all_stream_ids = await session_store.list_all_stream_ids()

        recovery_window = 600.0  # 只恢复最近 10 分钟内有活动的 session
        now = _time.time()
        recovered_count = 0

        for stream_id in all_stream_ids:
            try:
                session = await session_store.peek(stream_id)
                if session is None:
                    continue

                # 只检查最近有活动的 session
                if now - session.last_activity_at > recovery_window:
                    continue

                # 查询 DB 中该流最近的消息
                recent_msgs = await get_stream_messages(
                    stream_id=stream_id, limit=5
                )
                if not recent_msgs:
                    continue

                # 检查是否有比 session 最后活动时间更新的消息
                msg_times = []
                for m in recent_msgs:
                    t = getattr(m, "time", None) or getattr(m, "timestamp", None)
                    if isinstance(t, (int, float)):
                        msg_times.append(float(t))
                if not msg_times:
                    continue
                latest_msg_time = max(msg_times)

                if latest_msg_time > session.last_activity_at:
                    # 有未处理的消息，启动该流的 Tick 驱动器
                    await slm.start_stream_loop(stream_id)
                    recovered_count += 1
                    logger.info(
                        f"[NFC] 对话恢复：流 {stream_id[:8]} 检测到重启期间未处理消息，已触发恢复"
                    )
            except Exception as exc:
                logger.debug(f"[NFC] 对话恢复检查失败: stream={stream_id[:8]}, {exc}")
                continue

        if recovered_count > 0:
            logger.info(f"[NFC] 对话中断恢复完成：共恢复 {recovered_count} 个会话")
        else:
            logger.debug("[NFC] 对话中断恢复检查完成：无需恢复")

    def get_components(self) -> list[type]:
        """获取插件内所有组件类（enabled=false 时不注册 Chatter）。

        Chatter 是否注册由 ``config.general.enabled`` 控制：
        - True：注册 ``NeoFatumChatter``，由 chatter_manager 正常选中
        - False：不注册 ``NeoFatumChatter``，重启后 chatter_manager 选不到 NFC，
          自然回退到 DFC 等其他 chatter。其他组件（actions/handlers）仍正常注册，
          满足"插件可被发现和加载，但不参与 chatter 调度"的语义。
        """
        components: list[type] = [
            NFCReplyAction,
            DoNothingAction,
            ScheduleProactiveAction,
            QueryActivityPatternAction,
            RecordHabitAction,
            QueryHabitsAction,
            ProactiveHandler,
            VoiceCallHistoryHandler,
        ]
        config = self.config
        if isinstance(config, NFCConfig) and config.general.enabled:
            components.append(NeoFatumChatter)
        return components

    async def on_config_updated(self) -> None:
        """配置更新后动态注册/注销 Chatter。

        - ``enabled=True``：补注册 ``NeoFatumChatter``（首次加载时已注册则跳过）。
        - ``enabled=False``：注销 Chatter 组件，并清理所有运行中的 NFC active
          chatter 实例，再强制重启受影响的流循环，让 ``ChatterManager`` 重新选
          chatter（DFC 等才能接管）。否则旧的 NFC 生成器仍卡在
          ``_active_chatters`` / ``_chatter_genes`` 中，关闭开关后照样跳过 DFC。
        """
        from src.core.components.registry import get_global_registry
        from src.core.components.state_manager import get_global_state_manager
        from src.core.components.types import ComponentState, ComponentType, build_signature
        from src.core.managers.chatter_manager import get_chatter_manager
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        config = self.config
        if not isinstance(config, NFCConfig):
            return

        registry = get_global_registry()
        state_manager = get_global_state_manager()
        sig = build_signature(
            "neo_fatum_chatter", ComponentType.CHATTER, "neo_fatum_chatter"
        )

        if config.general.enabled:
            if sig not in registry.get_by_plugin("neo_fatum_chatter"):
                registry.register(NeoFatumChatter)
                await state_manager.set_state_async(sig, ComponentState.ACTIVE)
                logger.info("NFC Chatter 已动态注册（enabled=true）")
            return

        # enabled=False：注销组件 + 清 active + 重启流
        if sig in registry.get_by_plugin("neo_fatum_chatter"):
            registry.unregister(sig)
            await state_manager.set_state_async(sig, ComponentState.UNLOADED)
            logger.info("NFC Chatter 已动态注销（enabled=false）")

        chatter_manager = get_chatter_manager()
        stream_loop_manager = get_stream_loop_manager()
        active_chatters = chatter_manager.get_active_chatters()
        affected_streams = [
            stream_id
            for stream_id, chatter in active_chatters.items()
            if getattr(chatter, "chatter_name", "") == "neo_fatum_chatter"
        ]
        if not affected_streams:
            return

        for stream_id in affected_streams:
            chatter_manager.unregister_active_chatter(stream_id)
            try:
                await stream_loop_manager.restart_stream_loop(stream_id)
            except Exception as exc:
                logger.warning(
                    f"重启流 {stream_id[:8]} 失败（chatter 已注销，下次 tick 会自然重选）: {exc}"
                )
        logger.info(
            f"NFC 已关闭：清理 {len(affected_streams)} 个活跃流并触发重启以让出 chatter 调度"
        )
