"""本地 GUI 服务：127.0.0.1 上的一层薄 HTTP API + 单页界面。

只绑定回环地址；校验 Host/Origin 头，防止浏览器里的其他页面跨站调用本机接口。
没有账号、没有云、没有外发请求 —— 除非你自己去点那个按钮。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import __version__, config as cfgmod, history
from .organizer import apply_plan, archive_root, plan, watch_roots
from .watcher import WatchEvent, Watcher

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 4 * 1024 * 1024


class AppState:
    """GUI 的单一状态容器。"""

    def __init__(self) -> None:
        self.config: Dict[str, Any] = cfgmod.load_config()
        self.rules: List[Dict[str, Any]] = cfgmod.load_rules()
        self.events: Deque[WatchEvent] = deque(maxlen=200)
        self.preview: List[Dict[str, Any]] = []
        self.preview_at: float = 0.0
        self.lock = threading.Lock()
        self._watch_lock = threading.Lock()
        self.watcher = Watcher(
            self.config,
            self.rules,
            on_event=self._on_event,
            log=lambda msg: None,
        )

    # ------------------------------------------------------------

    def _on_event(self, event: WatchEvent) -> None:
        with self.lock:
            self.events.append(event)

    def reload(self) -> None:
        self.config = cfgmod.load_config()
        self.rules = cfgmod.load_rules()
        self.watcher.config = self.config
        self.watcher.rules = self.rules

    def refresh_preview(self) -> List[Dict[str, Any]]:
        decisions = plan(self.config, self.rules)
        self.preview = [d.to_dict() for d in decisions]
        self.preview_at = time.time()
        return self.preview

    def snapshot(self) -> Dict[str, Any]:
        root = archive_root(self.config)
        stats = history.stats()
        batches = [b.__dict__ for b in history.list_batches(limit=20)]
        pending = [d for d in self.preview if d.get("action") == "move"]
        skipped = [d for d in self.preview if d.get("action") != "move"]
        return {
            "version": __version__,
            "config": self.config,
            "home": str(Path.home()),
            "watch_dirs": [str(p) for p in watch_roots(self.config)],
            "archive_root": str(root),
            "archive_root_exists": root.exists(),
            "rules": self.rules,
            "preview": self.preview,
            "preview_at": self.preview_at,
            "counts": {
                "pending": len(pending),
                "skipped": len(skipped),
                "files_sorted": stats["files_sorted"],
                "batches": stats["batches"],
            },
            "history": batches,
            "watcher": {
                "running": self.watcher.running,
                "started_at": self.watcher.started_at,
                "last_tick": self.watcher.last_tick,
                "total_moved": self.watcher.total_moved,
                "last_error": self.watcher.last_error,
            },
            "events": [e.__dict__ for e in list(self.events)[-40:]][::-1],
            "paths": {
                "config": str(cfgmod.config_path()),
                "rules": str(cfgmod.rules_path()),
                "db": str(cfgmod.db_path()),
            },
        }


class Handler(BaseHTTPRequestHandler):
    server_version = f"DropInbox/{__version__}"
    state: AppState  # 由 make_server 注入

    # ---------------------------------------------------------- 基础工具

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype, _ = mimetypes.guess_type(str(path))
        if path.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif path.suffix in (".html", ".css"):
            ctype = f"{ctype or 'text/plain'}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin = origin.lower()
        return origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")

    # ---------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html")
            return
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            candidate = (WEB_DIR / name).resolve()
            if not str(candidate).startswith(str(WEB_DIR.resolve())) or not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(candidate)
            return
        if path == "/api/state":
            self._send_json(self.state.snapshot())
            return
        if path == "/api/history":
            self._send_json({"history": [b.__dict__ for b in history.list_batches(limit=50)]})
            return
        if path == "/api/preview":
            self.state.refresh_preview()
            self._send_json(self.state.snapshot())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_write("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._handle_write("POST")

    def _handle_write(self, method: str) -> None:
        if not self._host_ok() or not self._origin_ok():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = self.path.split("?")[0]
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        try:
            if path == "/api/rules" and method == "PUT":
                rules = payload.get("rules")
                if not isinstance(rules, list):
                    raise ValueError("rules 必须是数组")
                self.state.rules = rules
                cfgmod.save_rules(rules)
                self.state.watcher.rules = rules
                self._send_json({"ok": True, "rules": rules})
                return

            if path == "/api/config" and method == "PUT":
                incoming = payload.get("config") or {}
                merged = dict(self.state.config)
                merged.update({k: v for k, v in incoming.items() if k in cfgmod.DEFAULT_CONFIG})
                cfgmod.save_config(merged)
                self.state.reload()
                self.state.refresh_preview()
                self._send_json({"ok": True, "state": self.state.snapshot()})
                return

            if path == "/api/preview":
                self.state.refresh_preview()
                self._send_json(self.state.snapshot())
                return

            if path == "/api/apply":
                self.state.refresh_preview()
                wanted = payload.get("paths")
                decisions = [d for d in plan(self.state.config, self.state.rules) if d.action == "move"]
                if isinstance(wanted, list) and wanted:
                    wanted_set = {str(p) for p in wanted}
                    decisions = [d for d in decisions if str(d.path) in wanted_set]
                result = apply_plan(
                    decisions,
                    kind="gui",
                    note="界面应用",
                    keep_history=int(self.state.config.get("history_limit", 200) or 200),
                )
                self.state.refresh_preview()
                payload_out = self.state.snapshot()
                payload_out["result"] = result
                self._send_json(payload_out)
                return

            if path == "/api/undo":
                batch_id = payload.get("batch_id")
                if batch_id in (None, "", "last"):
                    batch_id = history.last_undoable_batch()
                if not batch_id:
                    self._send_json({"error": "没有可撤销的批次"}, HTTPStatus.BAD_REQUEST)
                    return
                result = history.undo_batch(int(batch_id))
                self.state.refresh_preview()
                payload_out = self.state.snapshot()
                payload_out["result"] = dict(result, batch_id=int(batch_id))
                self._send_json(payload_out)
                return

            if path == "/api/watch":
                action = str(payload.get("action") or "").lower()
                with self.state._watch_lock:
                    if action == "start":
                        if not self.state.watcher.running:
                            self.state.reload()
                            self.state.watcher.start()
                    elif action == "stop":
                        self.state.watcher.stop()
                    else:
                        raise ValueError("action 只能是 start 或 stop")
                self._send_json({"ok": True, "watcher": {"running": self.state.watcher.running}})
                return

            if path == "/api/reveal":
                target = Path(str(payload.get("path") or "")).expanduser()
                if target.exists():
                    _reveal(target)
                self._send_json({"ok": True})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # 兜底，别让 GUI 白屏
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def _reveal(path: Path) -> None:
    try:
        if os.name == "nt":
            if path.is_dir():
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                os.startfile(str(path.parent))  # type: ignore[attr-defined]
        elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
            os.system(f'open "{path.parent if path.is_file() else path}"')
        else:
            os.system(f'xdg-open "{path.parent if path.is_file() else path}"')
    except Exception:
        pass


def make_server(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    state = AppState()
    handler = type("BoundHandler", (Handler,), {"state": state})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), handler)
    httpd.daemon_threads = True
    return httpd


def serve(port: int = 8765, open_browser: bool = True) -> int:
    httpd = make_server(port=port)
    actual_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"DropInbox GUI 运行中: {url}")
    print("关闭此窗口或按 Ctrl+C 退出。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        state = getattr(httpd.RequestHandlerClass, "state", None)
        if state is not None:
            state.watcher.stop()
        httpd.server_close()
    return 0
