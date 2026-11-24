"""Disabled DGB must not keep AgentRouter's scheduling gate open."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GateSiteSelectionTests(unittest.TestCase):
    def test_disabled_dgb_does_not_block_completed_agentrouter(self):
        for enabled in (None, "false", "true"):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                ar = Path(directory) / "history.jsonl"
                dgb = Path(directory) / "history_dgb.jsonl"
                ar.write_text(json.dumps({"date_cst": "2026-11-17", "slot": "A", "success": True,
                                          "ts": "2026-11-17T05:00:00+08:00"}) + "\n", encoding="utf-8")
                dgb.write_text("", encoding="utf-8")
                env = {"HISTORY_FILES": f"{ar},{dgb}",
                       "DGB_ACCOUNTS": '[{"username":"test-user"}]'}
                if enabled is not None:
                    env["DGB_ENABLED"] = enabled
                with patch.dict(os.environ, env, clear=True):
                    spec = importlib.util.spec_from_file_location("isolated_gate", Path(__file__).with_name("gate.py"))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    rows = module.all_rows()
                    expected = set() if enabled == "true" else {"A"}
                    self.assertEqual(module.done_slots(rows, "2026-11-17"), expected)
                    self.assertEqual(len(module.HISTORY_FILES), 2 if enabled == "true" else 1)
    def test_runninghub_slot_requires_every_rh_account(self):
        with tempfile.TemporaryDirectory() as directory:
            ar = Path(directory) / "history.jsonl"
            rh = Path(directory) / "history_rh.jsonl"
            record = {"date_cst": "2026-11-17", "slot": "A", "success": True,
                      "ts": "2026-11-17T05:00:00+08:00"}
            ar.write_text(json.dumps(record) + "\n", encoding="utf-8")
            rh.write_text(json.dumps(dict(record, account="a@example.com")) + "\n", encoding="utf-8")
            accounts = [{"email": "a@example.com", "refresh_token": "t1"},
                        {"email": "b@example.com", "refresh_token": "t2"}]
            env = {"HISTORY_FILES": f"{ar},{rh}", "RH_ENABLED": "true",
                   "RH_ACCOUNTS": json.dumps(accounts), "DGB_ENABLED": "false",
                   "DGB_ACCOUNTS": '["someone-else"]'}
            with patch.dict(os.environ, env, clear=True):
                spec = importlib.util.spec_from_file_location("rh_gate", Path(__file__).with_name("gate.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.done_slots(module.all_rows(), "2026-11-17"), set())
                with rh.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(dict(record, account="b@example.com")) + "\n")
                self.assertEqual(module.done_slots(module.all_rows(), "2026-11-17"), {"A"})

    def test_runninghub_is_ignored_when_no_accounts_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            ar = Path(directory) / "history.jsonl"
            record = {"date_cst": "2026-11-17", "slot": "A", "success": True,
                      "ts": "2026-11-17T05:00:00+08:00"}
            ar.write_text(json.dumps(record) + "\n", encoding="utf-8")
            env = {"HISTORY_FILES": str(ar), "RH_ACCOUNTS": "", "DGB_ENABLED": "false"}
            with patch.dict(os.environ, env, clear=True):
                spec = importlib.util.spec_from_file_location("rh_gate2", Path(__file__).with_name("gate.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual([Path(p).name for p in module.HISTORY_FILES], ["history.jsonl"])
                self.assertEqual(module.done_slots(module.all_rows(), "2026-11-17"), {"A"})

    def test_enabled_gate_requires_all_plain_username_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            ar = Path(directory) / "history.jsonl"
            dgb = Path(directory) / "history_dgb.jsonl"
            record = {"date_cst": "2026-11-17", "slot": "A", "success": True,
                      "ts": "2026-11-17T05:00:00+08:00"}
            ar.write_text(json.dumps(record) + "\n", encoding="utf-8")
            dgb.write_text(json.dumps(dict(record, account="alice")) + "\n", encoding="utf-8")
            env = {"HISTORY_FILES": f"{ar},{dgb}", "DGB_ENABLED": "true",
                   "DGB_ACCOUNTS": '["alice","bob"]'}
            with patch.dict(os.environ, env, clear=True):
                spec = importlib.util.spec_from_file_location("account_gate", Path(__file__).with_name("gate.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.done_slots(module.all_rows(), "2026-11-17"), set())
                with dgb.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(dict(record, account="bob")) + "\n")
                self.assertEqual(module.done_slots(module.all_rows(), "2026-11-17"), {"A"})


if __name__ == "__main__":
    unittest.main()
