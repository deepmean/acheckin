"""Mengzhiji cookie-session check-in tests against a local HTTP server."""
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

import checkin_mzj as mzj

TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class MzjHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body, cookie=None):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.server.paths.append(("GET", path))
        cookie = self.headers.get("Cookie", "")
        email = self.server.cookies.get(cookie.removeprefix("novel_session="))
        if path == "/api/auth/me":
            if email:
                self.reply(200, {"ok": True, "user": {"email": email}})
            else:
                self.reply(401, {"ok": False, "code": "AUTH_REQUIRED"})
            return
        if path == "/api/rewards/daily-checkin":
            if not email:
                self.reply(401, {"ok": False, "code": "AUTH_REQUIRED"})
            elif self.server.mode.get(email) == "rate-limit":
                self.reply(429, {"ok": False, "code": "RATE_LIMIT"})
            else:
                self.reply(200, {"ok": True, "tokens": 50000, "expiryDays": 15,
                                 "claimedToday": bool(self.server.claimed.get(email, False))})
            return
        self.reply(404, {"ok": False, "code": "NOT_FOUND"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        self.server.paths.append(("POST", path))
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size) or b"{}")
        cookie = self.headers.get("Cookie", "")
        email = self.server.cookies.get(cookie.removeprefix("novel_session="))

        if path == "/api/auth/login":
            self.server.login_calls += 1
            expected = self.server.passwords.get(payload.get("email"))
            if expected is None or payload.get("password") != expected:
                self.reply(401, {"ok": False, "code": "INVALID_CREDENTIALS", "error": "登录失败"})
                return
            self.server.issued += 1
            new_cookie = f"fallback-cookie-{self.server.issued}"
            self.server.cookies[new_cookie] = payload["email"]
            self.reply(200, {"ok": True, "user": {"email": payload["email"]}},
                       f"novel_session={new_cookie}; Path=/; HttpOnly; SameSite=None")
            return

        if path == "/api/rewards/daily-checkin":
            if not email:
                self.reply(401, {"ok": False, "code": "AUTH_REQUIRED"})
            elif self.server.claimed.get(email):
                self.reply(200, {"ok": False, "error": "今天已经领过了", "tokens": 0})
            elif self.server.mode.get(email) == "claim-fails":
                self.reply(503, {"ok": False, "code": "TEMPORARY_FAILURE"})
            else:
                self.server.claimed[email] = True
                self.reply(200, {"ok": True, "tokens": 50000, "expiryDays": 15})
            return

        self.reply(404, {"ok": False, "code": "NOT_FOUND"})


class MengzhijiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MzjHandler)
        self.server.paths = []
        self.server.passwords = {"a@example.com": "password-a", "b@example.com": "password-b"}
        self.server.cookies = {"seed-a": "a@example.com", "seed-b": "b@example.com"}
        self.server.claimed = {}
        self.server.mode = {}
        self.server.issued = 0
        self.server.login_calls = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        accounts = [{"email": "a@example.com", "password": "password-a"},
                    {"email": "b@example.com", "password": "password-b"}]
        seeds = {"a@example.com": "seed-a", "b@example.com": "seed-b"}
        self.env = {
            "MZJ_ACCOUNTS": json.dumps(accounts),
            "MZJ_SESSIONS": json.dumps(seeds),
            "MZJ_SESSION_KEY": TEST_KEY,
            "MZJ_SESSION_STATE": str(self.root / "sessions.enc"),
            "MZJ_BASE": self.base,
            "SLOT": "A",
            "SLOT_TARGET": "",
        }
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch.object(mzj, "BASE", self.base))
        self.stack.enter_context(patch.object(mzj, "SLOT", "A"))
        self.stack.enter_context(patch.object(mzj, "HISTORY", self.root / "history_mzj.jsonl"))
        self.stack.enter_context(patch.object(mzj, "STATE_DIR", self.root))
        self.stack.enter_context(patch.object(mzj.time, "sleep"))

    def tearDown(self):
        self.stack.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = mzj.main()
        text = output.getvalue()
        for secret in ("seed-a", "seed-b", "fallback-cookie-", "password-a", "password-b"):
            self.assertNotIn(secret, text)
        return code, text

    def test_existing_cookie_sessions_claim_without_password_login(self):
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(self.server.claimed, {"a@example.com": True, "b@example.com": True})
        self.assertFalse(any(path == "/api/auth/login" for _, path in self.server.paths))

    def test_second_slot_checks_status_and_does_not_claim_again(self):
        self.assertEqual(self.run_main()[0], 0)
        claim_calls = sum(method == "POST" and path == "/api/rewards/daily-checkin"
                          for method, path in self.server.paths)
        with patch.object(mzj, "SLOT", "B"):
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(sum(method == "POST" and path == "/api/rewards/daily-checkin"
                             for method, path in self.server.paths), claim_calls)

    def test_unauthorized_cookie_falls_back_once_then_encrypted_cookie_reused(self):
        self.server.cookies.pop("seed-a")
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        state = (self.root / "sessions.enc").read_bytes()
        self.assertNotIn(b"fallback-cookie", state)
        self.assertNotIn(b"a@example.com", state)
        self.server.paths.clear()
        with patch.object(mzj, "SLOT", "B"):
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.login_calls, 1)
        self.assertFalse(any(path == "/api/auth/login" for _, path in self.server.paths))

    def test_429_never_triggers_password_fallback(self):
        self.server.mode["a@example.com"] = "rate-limit"
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.server.login_calls, 0)
        self.assertTrue(self.server.claimed.get("b@example.com"))

    def test_missing_key_prevents_fallback_login(self):
        self.server.cookies.pop("seed-a")
        os.environ.pop("MZJ_SESSION_KEY", None)
        code, text = self.run_main()
        self.assertEqual(code, 2)
        self.assertEqual(self.server.login_calls, 0)
        self.assertIn("MZJ_SESSION_KEY", text)

    def test_history_contains_no_session_data(self):
        self.assertEqual(self.run_main()[0], 0)
        rows = [json.loads(line) for line in
                (self.root / "history_mzj.jsonl").read_text("utf-8").splitlines() if line.strip()]
        self.assertEqual({row["account"] for row in rows}, {"a@example.com", "b@example.com"})
        self.assertTrue(all(row["success"] for row in rows))
        self.assertFalse(any("cookie" in json.dumps(row).lower() for row in rows))


if __name__ == "__main__":
    unittest.main()