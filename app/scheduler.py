"""定时生效调度器：启动恢复 + 守护线程轮询。

激活逻辑完全依赖数据库状态（scheduled + effective_at），不依赖进程内定时器，
因此：

* 重启后启动时立即扫描一次，错过生效时间的版本会被补激活（重启恢复）；
* 运行期由守护线程按固定间隔扫描；
* 多进程/多线程同时轮询时，规则仓储里的条件更新保证幂等且只有一方成功。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from .db import SessionLocal
from .rule_service import activate_due_scheduled


def _interval() -> float:
    return float(os.getenv("RULE_SCHEDULER_INTERVAL_SECONDS", "5"))


class RuleScheduler:
    def __init__(
        self,
        interval_seconds: float | None = None,
        session_factory=SessionLocal,
    ) -> None:
        self.interval = (
            interval_seconds if interval_seconds is not None else _interval()
        )
        self._session_factory = session_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def configure(
        self, *, interval_seconds: float | None = None, session_factory=None
    ) -> None:
        """测试或多数据库部署可替换轮询间隔与会话工厂。"""
        if interval_seconds is not None:
            self.interval = interval_seconds
        if session_factory is not None:
            self._session_factory = session_factory

    def run_once(self) -> list[str]:
        """扫描并激活所有到期版本。可安全地被并发/重复调用。"""
        db = self._session_factory()
        try:
            return activate_due_scheduled(db, datetime.now(timezone.utc))
        finally:
            db.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - 守护线程不能因单次错误退出
                pass

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            # 启动恢复：先同步跑一次，把停机期间到期的版本补激活。
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - 启动扫描失败不能阻止应用引导
                pass
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="rule-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=self.interval + 1)


scheduler = RuleScheduler()
