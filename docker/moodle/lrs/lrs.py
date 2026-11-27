"""EduPACS 一键集成栈内置轻量 LRS（演示/教学用）。

- 接收 EduPACS 投递的 xAPI statement（单条 JSON 对象或数组）
- 追加写入 /data/statements.jsonl，提供简单的查询页面
- 认证使用环境变量 XAPI_AUTH_HEADER 完整匹配（由 configure.py 生成）

生产环境可把 xapi.json 的 endpoint 换成正式 LRS（Learning Locker 等）。
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_FILE = Path(os.environ.get("XAPI_DATA_FILE", "/data/statements.jsonl"))
AUTH_HEADER = os.environ.get("XAPI_AUTH_HEADER", "")
PORT = int(os.environ.get("XAPI_PORT", "8001"))


def _auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    if not AUTH_HEADER:
        return True
    return handler.headers.get("Authorization", "") == AUTH_HEADER


def _cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Experience-API-Version")
    handler.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")


def _json(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[lrs] " + (fmt % args), flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        _cors(self)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("/statements", "/xapi/statements"):
            _json(self, 404, {"error": "not found"})
            return
        if not _auth_ok(self):
            _json(self, 401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception as exc:
            _json(self, 400, {"error": "invalid json", "detail": str(exc)[:200]})
            return

        statements = data if isinstance(data, list) else [data]
        if not all(isinstance(s, dict) for s in statements):
            _json(self, 400, {"error": "statement must be an object"})
            return
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            with DATA_FILE.open("a", encoding="utf-8") as f:
                for statement in statements:
                    statement.setdefault("stored", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    f.write(json.dumps(statement, ensure_ascii=False) + "\n")
            _json(self, 200, [s.get("id", "") for s in statements])
        except Exception as exc:
            _json(self, 500, {"error": "write failed", "detail": str(exc)[:200]})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("/statements", "/xapi/statements", "/", "/health"):
            _json(self, 404, {"error": "not found"})
            return
        if parsed.path.rstrip("/") == "/health":
            _json(self, 200, {"ok": True, "statements": self._count()})
            return
        # 查询接口只读且语句为匿名数据，便于教学现场快速核对；投递仍强制认证
        limit = int(parse_qs(parsed.query).get("limit", ["50"])[0] or 50)
        lines = []
        if DATA_FILE.exists():
            with DATA_FILE.open("r", encoding="utf-8", errors="replace") as f:
                lines = [ln for ln in f if ln.strip()][-max(1, min(limit, 500)):]
        items = []
        for ln in lines:
            try:
                items.append(json.loads(ln))
            except Exception:
                pass
        if parsed.path.rstrip("/") == "/":
            rows = "".join(
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    s.get("stored", ""),
                    (s.get("verb") or {}).get("display", {}).get("en-US", (s.get("verb") or {}).get("id", "")),
                    (s.get("object") or {}).get("id", ""),
                )
                for s in reversed(items)
            )
            html = (
                "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>EduPACS LRS</title><style>body{font-family:system-ui;margin:2rem;"
                "background:#f7f8fa;color:#222}table{border-collapse:collapse;width:100%;background:#fff}"
                "td,th{border:1px solid #ddd;padding:8px;text-align:left}th{background:#eef2f7}"
                "</style></head><body><h2>EduPACS xAPI 语句</h2>"
                "<p>共 {} 条，显示最近 {} 条。 <a href='/health'>健康检查</a></p>"
                "<table><tr><th>时间</th><th>动作</th><th>对象</th></tr>{}</table></body></html>"
            ).format(self._count(), len(items), rows)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        _json(self, 200, list(reversed(items)))

    @staticmethod
    def _count() -> int:
        if not DATA_FILE.exists():
            return 0
        with DATA_FILE.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for ln in f if ln.strip())


if __name__ == "__main__":
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("[lrs] listening on 0.0.0.0:{} -> {}".format(PORT, DATA_FILE), flush=True)
    server.serve_forever()
