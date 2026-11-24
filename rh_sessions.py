"""Encrypted store for RunningHub refresh tokens issued by password fallback.

Rotation is unnecessary for correctness: RunningHub issues independent refresh
tokens that keep working side by side, so a lost write only means the next run
falls back to the password again. The store exists to avoid exactly that.
"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class TokenStore:
    def __init__(self, path: Path, key: str):
        self.path = path
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError("RH_TOKEN_KEY_INVALID") from None
        self.tokens: dict[str, str] = {}
        if path.exists():
            try:
                document = json.loads(self.cipher.decrypt(path.read_bytes()))
                entries = document["tokens"]
                if not isinstance(entries, dict):
                    raise ValueError("invalid token map")
                self.tokens = {str(k): str(v) for k, v in entries.items()}
            except (InvalidToken, ValueError, TypeError, KeyError, OSError):
                raise RuntimeError("RH_TOKEN_STATE_INVALID") from None

    def get(self, email: str) -> str:
        return self.tokens.get(email, "")

    def set(self, email: str, token: str) -> None:
        self.tokens[email] = token
        self.save()

    def save(self) -> None:
        ciphertext = self.cipher.encrypt(json.dumps({"version": 1, "tokens": self.tokens}).encode("utf-8"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError:
            raise RuntimeError("RH_TOKEN_SAVE_FAILED") from None
        finally:
            tmp.unlink(missing_ok=True)