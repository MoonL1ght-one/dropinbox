"""目录监听。

刻意用轮询而不是 inotify / FSEvents / ReadDirectoryChangesW：
行为在三平台完全一致，零依赖，而且天然避开"文件还在写"的问题 ——
只有（大小, 修改时间）连续 settle_seconds 不变，才认为是下载完成。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .organizer import Decision, apply_plan, collect_files, plan

__all__ = ["Watcher", "WatchEvent"]

LogFn = Callable[[str], None]
BatchFn = Callable[[Dict[str, Any]], None]


@dataclass
class WatchEvent:
    at: str
    kind: str  # moved | skipped | error | start | stop
    detail: str


class Watcher:
    """后台线程轮询监听目录，稳定后自动归档。"""

    def __init__(
        self,
        config: Dict[str, Any],
        rules: List[Dict[str, Any]],
        dry_run: bool = False,
        on_batch: Optional[BatchFn] = None,
        on_event: Optional[Callable[[WatchEvent], None]] = None,
        log: Optional[LogFn] = None,
    ) -> None:
        self.config = config
        self.rules = rules
        self.dry_run = dry_run
        self.on_batch = on_batch
        self.on_event = on_event
        self._log = log or (lambda msg: None)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[int, float, float]] = {}

        self.started_at: Optional[float] = None
        self.last_tick: Optional[float] = None
        self.total_moved: int = 0
        self.total_batches: int = 0
        self.last_error: str = ""

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._pending.clear()
        self._thread = threading.Thread(target=self._loop, name="dropinbox-watcher", daemon=True)
        self._thread.start()
        self.started_at = time.time()
        self._emit("start", f"监听 {len(self.config.get('watch_dirs', []))} 个目录")
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        if not self.running:
            return False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        self.started_at = None
        self._emit("stop", "已停止监听")
        return True

    # ------------------------------------------------------------ 主循环

    def _loop(self) -> None:
        interval = float(self.config.get("poll_interval", 2.0) or 2.0)
        interval = max(0.5, min(interval, 60.0))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 单次失败不能让监听线程死掉
                self.last_error = str(exc)
                self._emit("error", str(exc))
            self._stop.wait(interval)

    def tick(self) -> List[Decision]:
        """扫描一轮，返回本轮已处理（或已预演）的决策。"""
        now = time.time()
        self.last_tick = now
        settle = float(self.config.get("settle_seconds", 1.5) or 1.5)

        try:
            files = collect_files(self.config)
        except OSError as exc:
            self.last_error = str(exc)
            return []

        alive = set()
        ready: List[Path] = []
        with self._lock:
            for path in files:
                key = str(path).lower()
                alive.add(key)
                try:
                    st = path.stat()
                except OSError:
                    continue
                signature = (st.st_size, st.st_mtime)
                prev = self._pending.get(key)
                if prev is None:
                    self._pending[key] = (signature[0], signature[1], now)
                elif (prev[0], prev[1]) != signature:
                    self._pending[key] = (signature[0], signature[1], now)
                elif now - prev[2] >= settle:
                    ready.append(path)
                    del self._pending[key]

            for key in list(self._pending):
                if key not in alive:
                    del self._pending[key]

        if not ready:
            return []

        decisions = plan(self.config, self.rules, paths=ready, now=now)
        moves = [d for d in decisions if d.action == "move"]
        if not moves:
            return decisions

        if self.dry_run:
            self.total_batches += 1
            self._emit("skipped", f"预演：{len(moves)} 个文件待归档（未执行）")
            return decisions

        result = apply_plan(
            decisions,
            kind="watch",
            note="自动监听",
            keep_history=int(self.config.get("history_limit", 200) or 200),
        )
        self.total_moved += int(result.get("moved", 0))
        self.total_batches += 1
        for item in result.get("items", []):
            self._emit("moved", f"{Path(item['src']).name} -> {item['dst']}")
        if self.on_batch:
            try:
                self.on_batch(result)
            except Exception:
                pass
        return decisions

    def _emit(self, kind: str, detail: str) -> None:
        self._log(detail)
        if self.on_event:
            try:
                self.on_event(
                    WatchEvent(
                        at=time.strftime("%Y-%m-%d %H:%M:%S"),
                        kind=kind,
                        detail=detail,
                    )
                )
            except Exception:
                pass
