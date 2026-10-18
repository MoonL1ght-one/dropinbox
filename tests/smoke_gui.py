"""GUI 后端冒烟测试：不依赖浏览器，直接打 HTTP 接口。"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dropinbox.server import make_server


def call(base, path, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype:
            return resp.status, json.loads(raw)
        return resp.status, raw


def main() -> int:
    httpd = make_server(port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    failures = []

    def check(label, fn):
        try:
            fn()
            print(f"  ok   {label}")
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            print(f"  FAIL {label}: {exc}")

    def t_index():
        status, body = call(base, "/")
        assert status == 200, status
        assert b"DropInbox" in body

    def t_css():
        status, body = call(base, "/static/style.css")
        assert status == 200 and b"--accent" in body

    def t_js():
        status, body = call(base, "/static/app.js")
        assert status == 200 and b"boot" in body

    def t_state():
        status, data = call(base, "/api/state")
        assert status == 200
        assert "counts" in data and "rules" in data
        assert isinstance(data["rules"], list) and data["rules"]

    def t_preview():
        status, data = call(base, "/api/preview", "POST", {})
        assert status == 200
        assert data["counts"]["pending"] >= 1, data["counts"]
        actions = {d["name"]: d for d in data["preview"]}
        assert actions["demo.pdf"]["action"] == "move", actions["demo.pdf"]
        assert actions["demo.pdf"]["target"].endswith("文档") \
            or "文档" in actions["demo.pdf"]["target"], actions["demo.pdf"]

    def t_apply():
        status, data = call(base, "/api/apply", "POST", {})
        assert status == 200, data
        assert data["result"]["moved"] >= 1, data["result"]
        assert data["counts"]["pending"] == 0, data["counts"]
        assert data["result"]["batch_id"]

    def t_undo():
        status, data = call(base, "/api/undo", "POST", {"batch_id": "last"})
        assert status == 200 and data["result"]["restored"] >= 1, data

    def t_rules_put():
        status, data = call(base, "/api/state")
        rules = data["rules"]
        rules.append({
            "id": "smoke",
            "name": "冒烟规则",
            "enabled": True,
            "priority": 500,
            "match": {"ext": ["smoke"]},
            "target": "测试/冒烟",
        })
        status, out = call(base, "/api/rules", "PUT", {"rules": rules})
        assert status == 200 and len(out["rules"]) == len(rules)
        assert any(r["id"] == "smoke" for r in out["rules"])

    def t_watch():
        status, out = call(base, "/api/watch", "POST", {"action": "start"})
        assert status == 200 and out["watcher"]["running"] is True, out
        status, out = call(base, "/api/watch", "POST", {"action": "stop"})
        assert status == 200 and out["watcher"]["running"] is False, out

    def t_history():
        status, data = call(base, "/api/history")
        assert status == 200 and isinstance(data["history"], list)

    def t_csrf():
        # 伪造 Host 头应被拒绝
        try:
            req = urllib.request.Request(
                base + "/api/state", headers={"Host": "evil.example.com"}
            )
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("伪造 Host 未被拦截")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code

    check("首页可访问", t_index)
    check("样式表可访问", t_css)
    check("脚本可访问", t_js)
    check("state 接口", t_state)
    check("preview 接口", t_preview)
    check("apply 接口", t_apply)
    check("undo 接口", t_undo)
    check("rules 保存", t_rules_put)
    check("watch 开关", t_watch)
    check("history 接口", t_history)
    check("Host 头校验", t_csrf)

    httpd.shutdown()
    httpd.server_close()

    if failures:
        print("\n失败项：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
