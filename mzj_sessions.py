"""Fernet-encrypted persistence for Mengzhiji HttpOnly session cookies."""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CookieStore:
    def __init__(self, path: Path, key: str, seeds: dict[str, str] | None = None):
        self.path = path
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError("MZJ_SESSION_KEY_INVALID") from None
        self.cookies: dict[str, str] = {}
        try:
            if path.exists():
                document = json.loads(self.cipher.decrypt(path.read_bytes()))
                if document.get("version") != 1 or not isinstance(document.get("cookies"), dict):
                    raise ValueError("invalid session document")
                self.cookies = {str(k): str(v) for k, v in document["cookies"].items()}
            for email, cookie in (seeds or {}).items():
                if email not in self.cookies and isinstance(cookie, str) and cookie:
                    self.cookies[email] = cookie
        except (InvalidToken, ValueError, TypeError, KeyError, OSError):
            raise RuntimeError("MZJ_SESSION_STATE_INVALID") from None

    def get(self, email: str) -> str:
        return self.cookies.get(email, "")

    def set(self, email: str, cookie: str) -> None:
        if not cookie:
            raise RuntimeError("MZJ_SESSION_COOKIE_MISSING")
        self.cookies[email] = cookie
        self.save()

    def save(self) -> None:
        ciphertext = self.cipher.encrypt(json.dumps({
            "version": 1, "cookies": self.cookies,
        }).encode("utf-8"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError:
            raise RuntimeError("MZJ_SESSION_SAVE_FAILED") from None
        finally:
            tmp.unlink(missing_ok=True)