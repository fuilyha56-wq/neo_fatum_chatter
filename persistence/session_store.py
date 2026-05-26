"""NFC 会话持久化存储。

仅负责 IO/锁/索引；状态结构定义在 ``domain.session_state``。
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.app.plugin_system.api.log_api import get_logger

from ..domain.session_state import NFCSession
from ..mental_log import MentalLog

logger = get_logger("NFC_session_store")


class NFCSessionStore:
    """NFC 会话持久化存储。

    使用 JSONStore 进行简单 JSON 文件持久化。
    Session 按 stream_id 索引。
    """

    def __init__(self, max_log_entries: int = 50) -> None:
        self._sessions: dict[str, NFCSession] = {}
        self._store_initialized = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._max_log_entries = max_log_entries

    def _get_lock(self, stream_id: str) -> asyncio.Lock:
        """获取指定 stream_id 的锁（惰性创建）。"""
        if stream_id not in self._locks:
            self._locks[stream_id] = asyncio.Lock()
        return self._locks[stream_id]

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """获取指定 stream_id 的互斥锁上下文。

        确保同一 stream 的 Session 读写串行化，
        防止 Scheduler 回调与 execute() 并发读写同一 Session。

        Args:
            stream_id: 流 ID

        Yields:
            None
        """
        async with self._get_lock(stream_id):
            yield

    async def _ensure_store(self) -> None:
        """延迟初始化 JSONStore。"""
        if self._store_initialized:
            return
        try:
            from src.kernel.storage import JSONStore

            self._json_store = JSONStore(
                storage_dir="data/neo_fatum_chatter/sessions"
            )
            self._store_initialized = True
        except ImportError:
            self._json_store = None
            self._store_initialized = True

    async def get_or_create(self, stream_id: str) -> NFCSession:
        """获取或创建 Session。

        注意：此方法不持有 per-stream 锁。调用方应使用 ``async with store.lock(stream_id)``
        包裹完整的读写周期以避免并发竞态。
        """
        if stream_id in self._sessions:
            return self._sessions[stream_id]

        await self._ensure_store()

        # 尝试从持久化加载
        if self._json_store is not None:
            try:
                data = await self._json_store.load(stream_id)
                if data and isinstance(data, dict):
                    session = NFCSession.from_dict(data, max_log_entries=self._max_log_entries)
                    self._sessions[stream_id] = session
                    return session
            except Exception as e:
                logger.warning(f"Session 加载失败 (stream={stream_id[:8]}): {e}")

        # 创建新 Session
        session = NFCSession(user_id="", stream_id=stream_id, platform="")
        session.mental_log = MentalLog(max_entries=self._max_log_entries)
        self._sessions[stream_id] = session
        return session

    async def save(self, session: NFCSession) -> None:
        """保存 Session 到持久化存储。

        注意：此方法不持有 per-stream 锁。调用方应使用 ``async with store.lock(stream_id)``
        包裹完整的读写周期以避免并发竞态。
        """
        self._sessions[session.stream_id] = session
        await self._ensure_store()

        if self._json_store is not None:
            try:
                await self._json_store.save(session.stream_id, session.to_dict())
                # 同步更新可读索引（stream_id → user_id + platform 的映射）
                await self._update_index(session)
            except Exception as e:
                logger.warning(
                    f"Session 持久化失败 (stream={session.stream_id[:8]}): {e}"
                )

        # 锁字典膨胀时定期清理不活跃的锁
        if len(self._locks) > 100:
            cleaned = self.cleanup_inactive_locks()
            if cleaned:
                logger.debug(f"清理了 {cleaned} 个不活跃的锁")

    async def get(self, stream_id: str) -> NFCSession | None:
        """获取 Session（不创建）。"""
        if stream_id in self._sessions:
            return self._sessions[stream_id]

        await self._ensure_store()
        if self._json_store is not None:
            try:
                data = await self._json_store.load(stream_id)
                if data and isinstance(data, dict):
                    session = NFCSession.from_dict(data, max_log_entries=self._max_log_entries)
                    self._sessions[stream_id] = session
                    return session
            except Exception as e:
                logger.warning(f"Session 加载失败 (stream={stream_id[:8]}): {e}")
        return None

    async def peek(self, stream_id: str) -> NFCSession | None:
        """从磁盘读取 Session 但不加入内存缓存。

        适用于只需查看持久化字段、不希望副作用地污染内存缓存的场景。
        若 session 已在内存中则直接返回（不重复加载）。

        Args:
            stream_id: 目标流 ID

        Returns:
            NFCSession 实例，或 None（文件不存在/解析失败）
        """
        if stream_id in self._sessions:
            return self._sessions[stream_id]

        await self._ensure_store()
        if self._json_store is not None:
            try:
                data = await self._json_store.load(stream_id)
                if data and isinstance(data, dict):
                    return NFCSession.from_dict(data, max_log_entries=self._max_log_entries)
            except Exception as e:
                logger.warning(f"Session peek 失败 (stream={stream_id[:8]}): {e}")
        return None

    def get_all_cached(self) -> dict[str, NFCSession]:
        """获取所有缓存中的 Session（不触发 IO）。"""
        return dict(self._sessions)

    def cleanup_inactive_locks(self) -> int:
        """清理不活跃 stream 的锁，释放内存。

        移除不在缓存中且当前未被持有的锁。

        Returns:
            int: 被清理的锁数量
        """
        stale = [
            sid for sid, lock in self._locks.items()
            if sid not in self._sessions and not lock.locked()
        ]
        for sid in stale:
            del self._locks[sid]
        return len(stale)

    async def list_all_stream_ids(self) -> list[str]:
        """列出所有已持久化的 stream_id。

        从 JSON 存储中读取所有会话文件名，
        用于在插件启动时预注册 VLM 跳过等批量操作。

        Returns:
            list[str]: 所有已知的 stream_id 列表
        """
        await self._ensure_store()
        if self._json_store is not None:
            try:
                all_ids = await self._json_store.list_all()
                # 过滤掉非 stream_id 的辅助文件（如 _index）
                return [sid for sid in all_ids if not sid.startswith("_")]
            except Exception as e:
                logger.warning(f"Session 列举失败: {e}")
                return []
        return []

    async def _update_index(self, session: NFCSession) -> None:
        """更新 _index.json 索引文件（stream_id → 可读标识映射）。

        每次 save() 后自动调用，让用户可通过 _index.json 对照文件名与 QQ 号。
        使用原子写入（写临时文件后 rename）防止写入中途崩溃导致文件损坏。
        """
        if self._json_store is None:
            return

        index_path = self._json_store.get_storage_dir() / "_index.json"

        # 读取现有索引
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index: dict[str, dict[str, str]] = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            index = {}

        # 更新当前 session 的条目
        entry: dict[str, str] = {
            "platform": session.platform,
            "user_id": session.user_id,
        }
        index[session.stream_id] = entry

        # 原子写入：先写临时文件，再 rename 覆盖
        try:
            dir_path = index_path.parent
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix="_index_", dir=str(dir_path)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(index, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(index_path))
            except Exception:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug(f"索引文件写入失败: {e}")
