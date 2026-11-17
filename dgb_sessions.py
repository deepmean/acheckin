"""Encrypted, crash-aware storage for existing DGB refresh sessions."""
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionStore:
    def __init__(self, path: Path, key: str, base: str, seeds: str = ""):
        self.path = path
        self.base = base
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError("DGB_SESSION_KEY_INVALID") from None
        self.entries = {}
        try:
            if path.exists():
                document = json.loads(self.cipher.decrypt(path.read_bytes()))
                if document.get("version") != 1 or document.get("base") != base:
                    raise ValueError("session store origin mismatch")
                self.entries = document["sessions"]
                if not isinstance(self.entries, dict):
                    raise ValueError("invalid sessions")
            seed_data = json.loads(seeds) if seeds.strip() else {}
            if not isinstance(seed_data, dict):
                raise ValueError("invalid session seeds")
            for user, value in seed_data.items():
                if not isinstance(user, str) or not isinstance(value, dict):
                    raise ValueError("invalid session seed")
                cookie = value.get("refresh_cookie")
                if not isinstance(cookie, str) or not cookie or any(ord(c) < 33 or ord(c) > 126 for c in cookie):
                    raise ValueError("invalid refresh cookie")
                sid = value.get("sid", "")
                if not isinstance(sid, str):
                    raise ValueError("invalid sid")
                digest = hashlib.sha256(cookie.encode("ascii")).hexdigest()
                previous = self.entries.get(user)
                # A changed Secret deliberately imports a new session. An unchanged
                # seed never overwrites a rotated or quarantined stored session.
                if previous is None or previous.get("seed_digest") != digest:
                    self.entries[user] = {
                        "refresh_cookie": cookie, "sid": sid, "seed_digest": digest,
                    }
        except (InvalidToken, ValueError, TypeError, KeyError, AttributeError, OSError):
            raise RuntimeError("DGB_SESSION_STATE_INVALID") from None

    def users(self):
        return list(self.entries)

    def get(self, user: str, checkpoint: str = "") -> dict:
        entry = self.entries.get(user)
        if not isinstance(entry, dict) or not entry.get("refresh_cookie"):
            raise RuntimeError("DGB_SESSION_REQUIRED")
        prepared = (checkpoint and entry.get("blocked") == "REFRESH_PREPARED"
                    and entry.get("prepared_for") == checkpoint)
        if entry.get("blocked") and not prepared:
            raise RuntimeError("DGB_SESSION_REIMPORT_REQUIRED")
        return dict(entry)

    def prepare(self, users: list[str], checkpoint: str) -> dict[str, str]:
        if not checkpoint:
            raise RuntimeError("DGB_REMOTE_CHECKPOINT_REQUIRED")
        errors = {}
        ready = []
        for user in users:
            try:
                self.get(user)
                ready.append(user)
            except RuntimeError as exc:
                errors[user] = str(exc)
        if users and not ready:
            raise RuntimeError("DGB_NO_USABLE_SESSION")
        for user in ready:
            self.entries[user].update(blocked="REFRESH_PREPARED", prepared_for=checkpoint)
        self.save()
        return errors

    def save(self):
        document = {"version": 1, "base": self.base, "sessions": self.entries}
        ciphertext = self.cipher.encrypt(json.dumps(document).encode("utf-8"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError:
            raise RuntimeError("DGB_SESSION_SAVE_FAILED") from None
        finally:
            tmp.unlink(missing_ok=True)

    def begin_refresh(self, user: str, checkpoint: str = "") -> dict:
        entry = self.get(user, checkpoint)
        if os.getenv("GITHUB_ACTIONS") == "true" and (
                not checkpoint or entry.get("prepared_for") != checkpoint
                or entry.get("blocked") != "REFRESH_PREPARED"):
            raise RuntimeError("DGB_REMOTE_CHECKPOINT_REQUIRED")
        self.entries[user]["blocked"] = "REFRESH_INCOMPLETE"
        self.entries[user].pop("prepared_for", None)
        self.save()
        return entry

    def rotated(self, user: str, cookie: str, sid: str):
        if not cookie or not sid:
            raise RuntimeError("DGB_REFRESH_RESPONSE_INVALID")
        self.entries[user].update(refresh_cookie=cookie, sid=sid)
        self.entries[user].pop("blocked", None)
        self.save()
