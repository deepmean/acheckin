"""Prove DGB never creates login sessions and persists refresh rotation."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import checkin_dgb as dgb

TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


class ReuseHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body, cookie=None):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlsplit(self.path).path
        self.server.paths.append(("POST", path))
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        if path == "/api/user/login":
            self.server.login_calls += 1
            self.reply(409, {"success": False, "code": "AUTH_SESSION_LIMIT", "message": "Conflict"})
            return
        if path == "/api/user/auth/logout":
            self.server.logout_calls += 1
            self.reply(200, {"success": True})
            return
        if path == "/api/user/auth/refresh":
            self.server.refresh_calls += 1
            if self.server.mode == "redirect":
                self.send_response(307)
                self.send_header("Location", "/api/user/auth/logout")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = cookie.get("new_api_refresh")
            sid = self.headers.get("X-Auth-Session")
            if (self.server.mode == "expired" or supplied is None
                    or supplied.value != self.server.cookie or sid not in (None, "existing-sid")):
                self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
                return
            self.server.cookie = f"rotated-refresh-{self.server.refresh_calls}"
            self.server.access = f"access-{self.server.refresh_calls}"
            self.reply(200, {"success": True, "data": {
                "access_token": self.server.access,
                "token_type": "Bearer",
                "session": {"sid": "different-sid" if self.server.mode == "different-sid" else "existing-sid"},
                "user": {"id": 1, "username": "other-user" if self.server.mode == "different-user" else "test-user"},
            }}, f"new_api_refresh={self.server.cookie}; Path=/api/user/auth; HttpOnly")
            return
        if self.headers.get("Authorization") != "Bearer " + self.server.access:
            self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
            return
        if path == "/api/user/checkin":
            self.server.checkin_calls += 1
            if self.server.mode == "redirect-checkin":
                self.send_response(307)
                self.send_header("Location", "/api/user/auth/logout")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.server.mode == "checkin-failed":
                self.reply(503, {"success": False, "message": "Temporary failure"})
                return
            self.server.checked_in = True
            self.reply(200, {"success": True, "data": {"quota_awarded": 15000000}})
            return
        self.reply(404, {"success": False})

    def do_GET(self):
        self.server.paths.append(("GET", urlsplit(self.path).path))
        if self.headers.get("Authorization") != "Bearer " + self.server.access:
            self.reply(401, {"success": False, "code": "AUTH_UNAUTHORIZED"})
        elif self.path == "/api/user/self":
            self.reply(200, {"success": True, "data": {"id": 1, "quota": 15000000}})
        else:
            self.reply(200, {"success": True, "data": {
                "enabled": True, "stats": {
                    "checked_in_today": self.server.checked_in,
                    "checkin_count": int(self.server.checked_in),
                    "total_quota": 15000000 if self.server.checked_in else 0,
                    "records": [],
                },
            }})


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReuseHandler)
        self.server.cookie = "original-refresh"
        self.server.access = "unissued"
        self.server.paths = []
        self.server.login_calls = 0
        self.server.logout_calls = 0
        self.server.refresh_calls = 0
        self.server.checkin_calls = 0
        self.server.checked_in = False
        self.server.mode = "normal"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {
            "DGB_ENABLED": "true", "DGB_SESSION_KEY": TEST_KEY,
            "DGB_SESSIONS": json.dumps({"test-user": {
                "refresh_cookie": "original-refresh", "sid": "existing-sid",
            }}),
            "DGB_SESSION_STATE": str(self.root / "sessions.enc"),
            "DGB_ACCOUNTS": json.dumps([{"username": "test-user", "password": "unused-password"}]),
        }
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch.object(dgb, "BASE", f"http://127.0.0.1:{self.server.server_port}"))
        self.stack.enter_context(patch.object(dgb, "SLOT", "manual"))
        self.stack.enter_context(patch.object(dgb, "SLOT_TARGET", ""))
        self.stack.enter_context(patch.object(dgb, "HISTORY", self.root / "history.jsonl"))
        self.stack.enter_context(patch.object(dgb, "STATE_DIR", self.root))
        self.stack.enter_context(patch.object(dgb.time, "sleep"))

    def tearDown(self):
        self.stack.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def execute(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = dgb.main()
        for secret in ("original-refresh", "rotated-refresh-", "unused-password", "access-"):
            self.assertNotIn(secret, output.getvalue())
        return code

    def test_disabled_with_password_config_sends_zero_requests(self):
        for value in (None, "false"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("DGB_ENABLED", None)
                else:
                    os.environ["DGB_ENABLED"] = value
                self.assertEqual(self.execute(), 0)
                self.assertEqual(self.server.paths, [])

    def test_missing_session_never_falls_back_to_password(self):
        os.environ.pop("DGB_SESSIONS")
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.paths, [])

    def test_repeated_runs_rotate_one_sid_without_login_or_logout(self):
        for _ in range(3):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.server.refresh_calls, 3)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(self.server.logout_calls, 0)
        self.assertEqual(self.server.checkin_calls, 1)
        ciphertext = (self.root / "sessions.enc").read_bytes()
        self.assertNotIn(b"rotated-refresh", ciphertext)
        self.assertNotIn(b"test-user", ciphertext)
        self.assertNotIn(b"unused-password", ciphertext)

    def test_expired_session_stops_future_refresh_without_password_login(self):
        self.server.mode = "expired"
        self.assertNotEqual(self.execute(), 0)
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.refresh_calls, 1)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(self.server.logout_calls, 0)

    def test_different_identity_is_quarantined_without_checkin(self):
        for mode in ("different-user", "different-sid"):
            with self.subTest(mode=mode):
                state = self.root / "sessions.enc"
                state.unlink(missing_ok=True)
                self.server.cookie = "original-refresh"
                self.server.mode = mode
                before_refresh = self.server.refresh_calls
                self.assertNotEqual(self.execute(), 0)
                self.assertEqual(self.server.checkin_calls, 0)
                self.assertNotEqual(self.execute(), 0)
                self.assertEqual(self.server.refresh_calls, before_refresh + 1)
                self.assertEqual(self.server.login_calls, 0)

    def test_refresh_redirect_is_not_followed(self):
        self.server.mode = "redirect"
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.refresh_calls, 1)
        self.assertEqual(self.server.logout_calls, 0)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(self.server.checkin_calls, 0)

    def test_checkin_redirect_is_not_followed(self):
        self.server.mode = "redirect-checkin"
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.logout_calls, 0)
        self.assertEqual(self.server.login_calls, 0)

    def test_ci_requires_a_committed_refresh_checkpoint(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["DGB_CHECKPOINT_ID"] = "run-1-attempt-1"
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.paths, [])

    def test_prepared_run_reuses_session_and_old_checkpoint_blocks_next_run(self):
        from dgb_sessions import SessionStore
        path = self.root / "sessions.enc"
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["DGB_CHECKPOINT_ID"] = "run-1-attempt-1"
        store = SessionStore(path, TEST_KEY, dgb.BASE, os.environ["DGB_SESSIONS"])
        store.prepare(["test-user"], "run-1-attempt-1")
        remote_checkpoint = path.read_bytes()
        self.assertEqual(self.execute(), 0)
        before = self.server.refresh_calls
        # Lose the runner's rotated state: origin still has only the in-flight
        # checkpoint. A new run must not replay the original cookie.
        path.write_bytes(remote_checkpoint)
        os.environ["DGB_CHECKPOINT_ID"] = "run-2-attempt-1"
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.refresh_calls, before)
        self.assertEqual(self.server.login_calls, 0)

    def test_rotation_survives_failed_checkin(self):
        self.server.mode = "checkin-failed"
        self.assertNotEqual(self.execute(), 0)
        self.server.mode = "normal"
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.server.refresh_calls, 2)
        self.assertEqual(self.server.login_calls, 0)
        self.assertEqual(self.server.logout_calls, 0)

    def test_wrong_encryption_key_sends_zero_new_requests(self):
        self.assertEqual(self.execute(), 0)
        before = list(self.server.paths)
        os.environ["DGB_SESSION_KEY"] = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="
        self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.paths, before)

    def test_storage_failure_prevents_refresh_and_checkin(self):
        with patch("os.replace", side_effect=OSError("storage unavailable")):
            self.assertNotEqual(self.execute(), 0)
        self.assertEqual(self.server.paths, [])


if __name__ == "__main__":
    unittest.main()
