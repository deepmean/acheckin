"""RunningHub check-in must never use passwords and must isolate accounts.

Runs against a local HTTP server so no real account is touched.
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
from urllib.parse import urlsplit

import checkin_rh as rh


class RhHandler(BaseHTTPRequestHandler):
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

    def token_of(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def do_POST(self):
        path = urlsplit(self.path).path
        self.server.paths.append(path)
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)

        if path == "/uc/pwdLogin" or path == "/uc/abroad/pwdLogin":
            self.server.login_calls += 1
            self.reply(200, {"code": 0, "msg": "success", "data": {
                "access_token": "should-not-happen", "refresh_token": "should-not-happen"}})
            return

        if path == "/uc/token/refresh":
            token = self.token_of()
            if token not in self.server.valid_refresh:
                self.reply(401, {"code": 401, "msg": "TOKEN_INVALID", "data": None})
                return
            email = self.server.valid_refresh[token]
            self.server.access[email] = f"access-{email}"
            self.reply(200, {"code": 0, "msg": "success", "data": {
                "access_token": f"access-{email}", "refresh_token": token, "expire_in": 2592000000}})
            return

        email = next((k for k, v in self.server.access.items() if v == self.token_of()), None)
        if email is None:
            self.reply(401, {"code": 401, "msg": "TOKEN_MISSION", "data": None})
            return

        if path == "/api/welfare/summary":
            self.reply(200, {"code": 0, "msg": "success", "data": {
                "enabled": True,
                "dailyGift": {"granted": self.server.granted.get(email, False), "amount": "30",
                              "unit": "POINT", "expireTime": "2026-11-25T00:00:00.000+08:00"},
            }})
            return

        if path == "/api/welfare/daily-gift/claim":
            if self.server.claim_fails.get(email):
                self.reply(200, {"code": 500, "msg": "CLAIM_FAILED", "data": None})
                return
            if self.server.granted.get(email):
                self.reply(200, {"code": 0, "msg": "success", "data": False})
                return
            self.server.granted[email] = True
            self.reply(200, {"code": 0, "msg": "success", "data": True})
            return

        if path == "/uc/point/summary":
            self.reply(200, {"code": 0, "msg": "success", "data": {
                "balancePoint": 30.0 if self.server.granted.get(email) else 0.0}})
            return

        self.reply(404, {"code": 404, "msg": "NOT_FOUND", "data": None})


class RunningHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RhHandler)
        self.server.paths = []
        self.server.login_calls = 0
        self.server.valid_refresh = {"refresh-a": "a@example.com", "refresh-b": "b@example.com"}
        self.server.access = {}
        self.server.granted = {}
        self.server.claim_fails = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        accounts = [{"email": "a@example.com", "refresh_token": "refresh-a"},
                    {"email": "b@example.com", "refresh_token": "refresh-b"}]
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.env = {
            "RH_ACCOUNTS": json.dumps(accounts),
            "RH_BASE": self.base,
            "SLOT": "A",
            "SLOT_TARGET": "",
            "RH_STATE": str(self.root / "history_rh.jsonl"),
        }
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch.object(rh, "BASE", self.base))
        self.stack.enter_context(patch.object(rh, "SLOT", "A"))
        self.stack.enter_context(patch.object(rh.time, "sleep"))
        self.stack.enter_context(patch.object(rh, "HISTORY", self.root / "history_rh.jsonl"))
        self.stack.enter_context(patch.object(rh, "STATE_DIR", self.root))

    def tearDown(self):
        self.stack.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rh.main()
        text = out.getvalue()
        for secret in ("refresh-a", "refresh-b", "should-not-happen"):
            self.assertNotIn(secret, text)
        return code, text

    def test_never_calls_password_login(self):
        self.run_main()
        self.assertEqual(self.server.login_calls, 0)
        self.assertFalse([p for p in self.server.paths if "pwdLogin" in p])

    def test_claims_once_then_skips_already_granted(self):
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.granted, {"a@example.com": True, "b@example.com": True})
        claims_before = self.server.paths.count("/api/welfare/daily-gift/claim")
        self.assertEqual(claims_before, 2)

        # Second slot the same day: granted already, so no further claim.
        with patch.object(rh, "SLOT", "B"):
            code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths.count("/api/welfare/daily-gift/claim"), claims_before)
        self.assertEqual(self.server.login_calls, 0)
        self.assertIn("已领取", text)

    def test_invalid_token_fails_one_account_only(self):
        self.server.valid_refresh.pop("refresh-a")
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(self.server.granted.get("b@example.com"))
        self.assertFalse(self.server.granted.get("a@example.com", False))
        self.assertIn("a@example.com", text)

    def test_claim_failure_is_reported_not_swallowed(self):
        self.server.claim_fails["a@example.com"] = True
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(self.server.granted.get("b@example.com"))

    def test_same_slot_is_deduplicated_by_history(self):
        self.run_main()
        refresh_calls = self.server.paths.count("/uc/token/refresh")
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths.count("/uc/token/refresh"), refresh_calls)

    def test_missing_accounts_config_fails_closed(self):
        os.environ["RH_ACCOUNTS"] = ""
        code, _ = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.paths, [])

    def test_malformed_accounts_config_fails_closed(self):
        os.environ["RH_ACCOUNTS"] = '{"not":"a list"}'
        code, _ = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.paths, [])

    def test_history_records_account_and_slot(self):
        self.run_main()
        rows = [json.loads(line) for line in
                (self.root / "history_rh.jsonl").read_text("utf-8").splitlines() if line.strip()]
        self.assertEqual({r["account"] for r in rows}, {"a@example.com", "b@example.com"})
        self.assertTrue(all(r["slot"] == "A" and r["success"] for r in rows))
        self.assertTrue(all("refresh" not in json.dumps(r) for r in rows))


if __name__ == "__main__":
    unittest.main()