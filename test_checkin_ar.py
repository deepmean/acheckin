"""AgentRouter must log in at most once per day and release the session.

Login on that site IS the check-in, so a refresh-only flow is impossible for
the reward itself. What is avoidable: a second login in the same day (slot B
after slot A succeeded) and leaving the created session active.
"""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import checkin


class ArHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "session=test-session; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.server.paths.append(self.path)
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        if self.path.startswith("/api/user/login"):
            self.server.login_calls += 1
            self.reply({"success": True, "message": "ok", "data": {
                "id": 4242, "username": "tester", "checked_in": True,
                "quota": 100, "used_quota": 1, "group": "default"}})
            return
        if self.path == "/api/user/auth/logout":
            self.server.logout_calls += 1
            status = getattr(self.server, "logout_status", 404)
            if status == 200:
                self.reply({"success": True, "message": "ok"})
            else:
                self.reply({"success": False, "message": "missing"}, status)
            return
        if self.path == "/api/user/logout":
            self.server.logout_calls += 1
            status = getattr(self.server, "logout_status", 200)
            if status == 200:
                self.reply({"success": True, "message": "ok"})
            else:
                self.reply({"success": False, "message": "boom"}, status)
            return
        self.reply({"success": False, "message": "not found"}, 404)

    def do_GET(self):
        if self.path == "/api/user/self":
            self.reply({"success": True, "data": {"quota": 100, "used_quota": 1, "group": "default"}})
            return
        self.reply({"success": False}, 404)


class AgentRouterLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ArHandler)
        self.server.paths = []
        self.server.login_calls = 0
        self.server.logout_calls = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(checkin, "BASE", self.base))
        self.stack.enter_context(patch.object(checkin, "USERNAME", "tester@example.com"))
        self.stack.enter_context(patch.object(checkin, "PASSWORD", "test-password"))
        self.stack.enter_context(patch.object(checkin, "SLOT", "A"))
        self.stack.enter_context(patch.object(checkin, "SLOT_TARGET", ""))
        self.stack.enter_context(patch.object(checkin, "HISTORY", self.root / "history.jsonl"))
        self.stack.enter_context(patch.object(checkin, "STATE_DIR", self.root))
        self.stack.enter_context(patch.dict(os.environ, {
            "CHECKIN_PROXY": "", "HTTPS_PROXY": "", "https_proxy": "",
            "ALL_PROXY": "", "HTTP_PROXY": "", "http_proxy": "",
        }))
        self.stack.enter_context(patch.object(checkin.time, "sleep"))

    def tearDown(self):
        self.stack.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checkin.main()
        return code, out.getvalue()

    def test_login_then_logout_releases_the_session(self):
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        self.assertEqual(self.server.logout_calls, 2)  # first path 404, fallback succeeds

    def test_logout_failure_does_not_fail_the_check_in(self):
        self.server.logout_status = 500
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        self.assertIn("未提供可用的注销接口", text)

    def test_second_slot_same_day_does_not_log_in_again(self):
        self.assertEqual(self.run_main()[0], 0)
        with patch.object(checkin, "SLOT", "B"):
            code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        self.assertIn("今日已签到", text)
        rows = [json.loads(line) for line in
                (self.root / "history.jsonl").read_text("utf-8").splitlines() if line.strip()]
        self.assertEqual({r["slot"] for r in rows}, {"A", "B"})
        self.assertTrue(all(r["success"] for r in rows))

    def test_logout_failure_does_not_fail_the_check_in(self):
        self.server.logout_status = 500
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        self.assertIn("未提供可用的注销接口", text)


if __name__ == "__main__":
    unittest.main()