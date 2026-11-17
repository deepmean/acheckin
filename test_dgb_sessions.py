"""Test encrypted session persistence without any real account access."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import checkin_dgb as dgb
from dgb_sessions import SessionStore

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
BASE = "https://freeapi.dgbmc.top"
SEED = json.dumps({"test-user": {"refresh_cookie": "test-refresh", "sid": "test-sid"}})


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict("os.environ", {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sessions.enc"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, seed=SEED, base=BASE):
        return SessionStore(self.path, KEY, base, seed)

    def test_refresh_in_progress_is_quarantined_across_runs(self):
        self.store().begin_refresh("test-user")
        with self.assertRaisesRegex(RuntimeError, "DGB_SESSION_REIMPORT_REQUIRED"):
            self.store().get("test-user")

    def test_original_secret_does_not_overwrite_rotated_cookie(self):
        store = self.store()
        store.begin_refresh("test-user")
        store.rotated("test-user", "rotated-cookie", "test-sid")
        self.assertEqual(self.store().get("test-user")["refresh_cookie"], "rotated-cookie")

    def test_explicitly_updated_secret_recovers_quarantined_session(self):
        self.store().begin_refresh("test-user")
        updated = json.dumps({"test-user": {"refresh_cookie": "replacement-cookie", "sid": "replacement-sid"}})
        self.assertEqual(self.store(updated).get("test-user")["sid"], "replacement-sid")

    def test_state_is_bound_to_the_original_site(self):
        self.store().save()
        with self.assertRaisesRegex(RuntimeError, "DGB_SESSION_STATE_INVALID"):
            self.store(base="https://other.example")

    def test_invalid_ciphertext_has_no_plaintext_fallback(self):
        self.path.write_bytes(b'{"refresh_cookie":"plaintext-test-cookie"}')
        with self.assertRaisesRegex(RuntimeError, "DGB_SESSION_STATE_INVALID"):
            self.store()

    def test_account_loader_ignores_legacy_passwords(self):
        config = {"DGB_ACCOUNTS": '[{"username":"test-user","password":"discard-this"}]'}
        with patch.dict("os.environ", config, clear=True):
            self.assertEqual(dgb.load_accounts(), [{"username": "test-user"}])

    def test_one_quarantined_account_does_not_block_healthy_account(self):
        seed = json.dumps({
            "blocked-user": {"refresh_cookie": "blocked-cookie", "sid": "blocked-sid"},
            "test-user": {"refresh_cookie": "healthy-cookie", "sid": "healthy-sid"},
        })
        store = self.store(seed)
        store.begin_refresh("blocked-user")
        store = self.store(seed)
        errors = store.prepare(["blocked-user", "test-user"], "next-run")
        self.assertIn("blocked-user", errors)
        self.assertEqual(store.get("test-user", "next-run")["sid"], "healthy-sid")
        with self.assertRaisesRegex(RuntimeError, "DGB_SESSION_REIMPORT_REQUIRED"):
            store.get("blocked-user", "next-run")

    def test_missing_key_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "DGB_SESSION_KEY_INVALID"):
            SessionStore(self.path, "", BASE, SEED)


if __name__ == "__main__":
    unittest.main()
