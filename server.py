"""確認画面のサーバー。

使い方:
    python koutsu.py review 事件フォルダ [--port N] [--no-browser]

このPCの中だけで動く小さなWebサーバーを立て、ブラウザで画面を開く。
外部には一切公開しない（127.0.0.1 だけで待ち受け、起動のたびに作る合言葉がないと応答しない）。
外に通信するのは、画面で「AIで読む」「AIで下書き」を押したときの Bedrock だけ（設定で使うにしたときだけ）。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import AppError, CaseApp

UI_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "ui.html"
MAX_BODY = 20 * 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'")


def make_handler(app: CaseApp, server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "koutsu-sojou"

        def log_message(self, fmt, *args):  # 画面に出さない（URLに合言葉が入るため）
            pass

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authed(self) -> bool:
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return "tok" in c and secrets.compare_digest(c["tok"].value, app.token)

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _deny(self):
            self._send(403, "許可されていません".encode("utf-8"), "text/plain; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._deny()
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                t = q.get("t", [""])[0]
                if not (self._authed() or secrets.compare_digest(t, app.token)):
                    return self._deny()
                html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {
                    "Set-Cookie": f"tok={app.token}; HttpOnly; SameSite=Strict; Path=/",
                    "Content-Security-Policy": CSP})
            if not self._authed():
                return self._deny()
            try:
                if url.path == "/api/data":
                    return self._json(200, app.data())
                if url.path == "/api/page":
                    png = app.page_png(q.get("rel", [""])[0], int(q.get("p", ["1"])[0]))
                    return self._send(200, png, "image/png")
            except (AppError, ValueError) as e:
                return self._json(400, {"error": str(e)})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not (self._host_ok() and self._authed()
                    and secrets.compare_digest(self.headers.get("X-Token", ""), app.token)):
                return self._deny()
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                return self._json(400, {"error": "Content-Length が正しくありません"})
            if n > MAX_BODY:
                return self._json(413, {"error": "大きすぎます"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "JSONが読めません"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "JSONの形が正しくありません"})
            path = urlparse(self.path).path
            s = lambda k: str(body.get(k, ""))  # noqa: E731
            try:
                if path == "/api/read":
                    return self._json(200, app.read_file(s("rel"), bool(body.get("force"))))
                if path == "/api/file":
                    return self._json(200, app.file_view(s("rel")))
                if path == "/api/ai_read":
                    return self._json(200, app.ai_read(s("rel")))
                if path == "/api/decide":
                    return self._json(200, app.decide(s("rel"), body.get("decisions") or {}))
                if path == "/api/save_case":
                    return self._json(200, app.save_case(body.get("case")))
                if path == "/api/settings":
                    return self._json(200, app.save_settings(body.get("settings")))
                if path == "/api/ai_draft":
                    return self._json(200, app.ai_draft(s("what"), body.get("keys") or []))
                if path == "/api/decide_draft":
                    return self._json(200, app.decide_draft(s("id"), bool(body.get("accepted")), s("body")))
                if path == "/api/build":
                    return self._json(200, app.build(bool(body.get("full"))))
                if path == "/api/open":
                    return self._json(200, app.open(s("which")))
                if path == "/api/quit":
                    self._json(200, {"ok": True})
                    threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                    return
            except AppError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # 想定外でも画面に理由を出す
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(404, {"error": "not found"})

    return Handler


def serve(app: CaseApp, port: int = 0, open_browser: bool = True):
    """サーバーを立てる。戻り値: (server, url)。server.serve_forever() で待ち受ける。"""
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, ref))
    ref["server"] = server
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={app.token}"
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    return server, url


def run(app: CaseApp, port: int = 0, open_browser: bool = True) -> None:
    server, url = serve(app, port, open_browser)
    print()
    print("画面を開きます。開かないときは、次のアドレスをブラウザに貼り付けてください：")
    print(f"  {url}")
    print("終わったら画面の「終了」を押してください（この画面で Ctrl+C でも止まります）。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="koutsu review", description="事件フォルダの証拠PDFから、交通事故の訴状と共通書式を作る")
    ap.add_argument("case_dir", help="事件フォルダ（証拠PDFを入れたフォルダ）")
    ap.add_argument("--port", type=int, default=0, help="待ち受けるポート（既定は空いているもの）")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = ap.parse_args(argv)
    try:
        app = CaseApp(args.case_dir)
    except AppError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"事件フォルダ: {app.case_dir}")
    print(f"証拠PDF: {len(app.pdfs())} 件")
    run(app, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
