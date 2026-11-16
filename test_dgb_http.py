"""Exercise the DGB session lifecycle over real local HTTP requests."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

import checkin_dgb as dgb


class DgbHandler(BaseHTTPRequestHandler):
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

    def authorized(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return token in self.server.active

    def do_POST(self):
        path = urlsplit(self.path).path
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size)) if size else {}
        if path == "/api/user/login":
            self.server.login_attempts += 1
            if self.server.active:
                self.reply(409, {"success": False, "code": "AUTH_SESSION_LIMIT", "message": "Conflict"})
                return
            if payload != {"username": "test-user", "password": "test-password"}:
                self.reply(401, {"success": False, "code": "AUTH_INVALID_CREDENTIALS"})
                return
            self.server.issued += 1
            token = f"test-token-{self.server.issued}"
            self.server.active.add(token)
            self.server.last_token = token
            self.reply(200, {
                "success": True,
                "data": {"access_token": token, "token_type": "Bearer", "user": {"id": 1}},
            }, "new_api_refresh=test-refresh; Path=/; HttpOnly")
            return
        if not self.authorized():
            self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
            return
        if path == "/api/user/auth/logout":
            if "new_api_refresh=test-refresh" not in self.headers.get("Cookie", ""):
                self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
                return
            self.server.active.discard(self.server.last_token)
            self.server.revoked += 1
            self.reply(200, {"success": True, "data": {"cookie_cleared": True}},
                       "new_api_refresh=; Max-Age=0; Path=/")
            return
        if path == "/api/user/checkin":
            if self.server.mode == "malformed":
                self.reply(200, [])
            elif self.server.mode == "failed":
                self.reply(503, {"success": False, "message": "Temporary failure"})
            else:
                self.server.checked_in = True
                self.reply(200, {"success": True, "data": {"quota_awarded": 15000000}})
            return
        self.reply(404, {"success": False})

    def do_GET(self):
        if not self.authorized():
            self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
            return
        if self.path == "/api/user/self":
            self.reply(200, {"success": True, "data": {"quota": 15000000 if self.server.checked_in else 0}})
        elif self.path == "/api/user/checkin":
            self.reply(200, {"success": True, "data": {
                "enabled": True,
                "min_quota": 15000000,
                "max_quota": 25555000,
                "stats": {
                    "checked_in_today": self.server.checked_in,
                    "checkin_count": int(self.server.checked_in),
                    "total_quota": 15000000 if self.server.checked_in else 0,
                    "records": [],
                },
            }})
        else:
            self.reply(404, {"success": False})


class DgbHttpSessionTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DgbHandler)
        self.server.active = set()
        self.server.checked_in = False
        self.server.mode = "success"
        self.server.issued = 0
        self.server.revoked = 0
        self.server.login_attempts = 0
        self.server.last_token = ""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.base_patch = patch.object(dgb, "BASE", self.base)
        self.slot_patch = patch.object(dgb, "SLOT", "manual")
        self.base_patch.start()
        self.slot_patch.start()
        self.account = {"username": "test-user", "password": "test-password"}

    def tearDown(self):
        self.slot_patch.stop()
        self.base_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def execute(self):
        return dgb.run_account(self.account, "2026-11-16", [])

    def assert_revoked(self):
        self.assertEqual(self.server.active, set())
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(self.base + "/api/user/self", headers={
                "Authorization": f"Bearer {self.server.last_token}",
            }, timeout=5)
        self.assertEqual(response.status_code, 401)

    def test_repeated_runs_keep_active_sessions_at_zero(self):
        for _ in range(3):
            result = self.execute()
            self.assertTrue(result["success"])
            self.assertTrue(result["checked_in_today"])
            self.assert_revoked()
        self.assertEqual(self.server.issued, 3)
        self.assertEqual(self.server.revoked, 3)

    def test_failed_checkin_still_revokes_its_session(self):
        self.server.mode = "failed"
        self.assertFalse(self.execute()["success"])
        self.assert_revoked()

    def test_malformed_checkin_still_revokes_its_session(self):
        self.server.mode = "malformed"
        with self.assertRaises(AttributeError):
            self.execute()
        self.assert_revoked()

    def test_session_limit_does_not_repeat_login(self):
        self.server.active.add("existing-session")
        result = self.execute()
        self.assertFalse(result["success"])
        self.assertIn("AUTH_SESSION_LIMIT", result["error"])
        self.assertEqual(self.server.login_attempts, 1)
        self.assertEqual(self.server.active, {"existing-session"})


if __name__ == "__main__":
    unittest.main()
