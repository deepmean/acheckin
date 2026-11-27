#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mengzhiji.net daily check-in using HttpOnly session cookies first.

Observed frontend API (2026-11-27):
  GET  https://api.mengzhiji.net/api/rewards/daily-checkin
       -> {ok, claimedToday, tokens, expiryDays}
  POST same path (credentials: include)
       -> {ok, claimed, tokens}
  POST /api/auth/login {email, password} sets HttpOnly novel_session cookie.

No refresh-token endpoint was found. Existing cookies are reused; password
login is allowed only after an explicit HTTP 401 and the new cookie is saved
with Fernet encryption. Network errors, 403, 429 and 5xx never trigger login.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

BASE = (os.getenv("MZJ_BASE") or "https://api.mengzhiji.net").rstrip("/")
SLOT = (os.getenv("SLOT") or "manual").strip() or "manual"
SLOT_TARGET = (os.getenv("SLOT_TARGET") or "").strip()
TG_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.getenv("TG_CHAT_ID") or "").strip()

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HISTORY = STATE_DIR / "history_mzj.jsonl"
CST = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(message: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] [mzj] {message}", flush=True)


def _decode_json_env(name: str) -> object:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        raise RuntimeError(f"{name} 为空")
    if not raw.startswith(("[", "{")):
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            decoded = ""
        if decoded.lstrip().startswith(("[", "{")):
            raw = decoded
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"{name} 不是合法 JSON/base64 JSON") from None


def load_accounts() -> list[dict]:
    parsed = _decode_json_env("MZJ_ACCOUNTS")
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("MZJ_ACCOUNTS 必须是非空数组")
    result, seen = [], set()
    for item in parsed:
        if not isinstance(item, dict):
            raise RuntimeError("MZJ_ACCOUNTS 含非对象条目")
        email = str(item.get("email") or "").strip()
        password = str(item.get("password") or "")
        if not email:
            raise RuntimeError("MZJ_ACCOUNTS 缺少 email")
        if email in seen:
            continue
        seen.add(email)
        result.append({"email": email, "password": password})
    return result


def load_seeds() -> dict[str, str]:
    if not (os.getenv("MZJ_SESSIONS") or "").strip():
        return {}
    parsed = _decode_json_env("MZJ_SESSIONS")
    if isinstance(parsed, list):
        seeds = {}
        for item in parsed:
            if isinstance(item, dict) and item.get("email") and item.get("cookie"):
                seeds[str(item["email"]).strip()] = str(item["cookie"])
        return seeds
    if isinstance(parsed, dict):
        return {str(k).strip(): str(v) for k, v in parsed.items()
                if isinstance(v, str) and v}
    raise RuntimeError("MZJ_SESSIONS 必须是 Cookie 映射或对象数组")


def history_rows() -> list[dict]:
    if not HISTORY.exists():
        return []
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def already_done(rows: list[dict], today: str, slot: str, email: str) -> bool:
    return any(r.get("date_cst") == today and r.get("slot") == slot
               and r.get("account") == email and r.get("success") for r in rows)


def append_history(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_step_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
        except OSError:
            pass


def tg_notify(message: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": message,
                            "disable_web_page_preview": True}, timeout=20)
    except requests.RequestException as exc:
        log(f"Telegram 推送失败：{type(exc).__name__}")


def wait_for_target() -> None:
    if not SLOT_TARGET:
        return
    try:
        target = dt.datetime.fromisoformat(SLOT_TARGET)
    except ValueError:
        return
    if target.tzinfo is None:
        target = target.replace(tzinfo=CST)
    delta = (target - now_cst()).total_seconds()
    if 0 < delta <= 900:
        log(f"等待到目标时间 {target:%H:%M:%S}（{delta:.0f}s）")
        time.sleep(delta)


def new_session(cookie: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://mengzhiji.net",
        "Referer": "https://mengzhiji.net/",
    })
    if cookie:
        host = urlsplit(BASE).hostname
        session.cookies.set("novel_session", cookie, domain=host, path="/",
                            secure=urlsplit(BASE).scheme == "https")
    return session


def request_json(session: requests.Session, method: str, path: str,
                 body: dict | None = None):
    response = session.request(method, BASE + path, json=body,
                               timeout=30, allow_redirects=False)
    if 300 <= response.status_code < 400:
        raise RuntimeError(f"{path} 重定向 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f"{path} 返回非 JSON HTTP {response.status_code}") from None
    return response, payload


def login(s: requests.Session, email: str, password: str) -> str:
    if not password:
        raise RuntimeError("MZJ_PASSWORD_REQUIRED")
    response, body = request_json(s, "POST", "/api/auth/login",
                                  {"email": email, "password": password})
    if response.status_code != 200 or not isinstance(body, dict) or not body.get("user"):
        code = body.get("code", "LOGIN_FAILED") if isinstance(body, dict) else "LOGIN_FAILED"
        raise RuntimeError(f"MZJ_LOGIN_REJECTED [{code}]")
    cookie = response.cookies.get("novel_session") or s.cookies.get("novel_session")
    if not cookie:
        raise RuntimeError("MZJ_LOGIN_COOKIE_MISSING")
    return cookie


def ensure_authenticated(email: str, password: str, cookie: str, store):
    s = new_session(cookie)
    try:
        response, body = request_json(s, "GET", "/api/auth/me")
    except (requests.RequestException, RuntimeError) as exc:
        s.close()
        raise RuntimeError(f"MZJ_AUTH_CHECK_FAILED {type(exc).__name__}") from None

    if response.status_code == 200 and isinstance(body, dict) and body.get("ok"):
        user_email = str((body.get("user") or {}).get("email") or "").strip().lower()
        if user_email and user_email != email.lower():
            s.close()
            raise RuntimeError("MZJ_SESSION_ACCOUNT_MISMATCH")
        return s

    if response.status_code != 401:
        s.close()
        code = body.get("code", response.status_code) if isinstance(body, dict) else response.status_code
        raise RuntimeError(f"MZJ_AUTH_REJECTED [{code}]")

    # Only explicit unauthenticated responses may trigger the password fallback.
    s.close()
    if not password:
        raise RuntimeError("MZJ_SESSION_EXPIRED_PASSWORD_MISSING")
    s = new_session()
    try:
        new_cookie = login(s, email, password)
        store.set(email, new_cookie)
        log(f"{email}: 会话失效，已密码兜底一次并加密保存新会话")
        response, body = request_json(s, "GET", "/api/auth/me")
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError("MZJ_LOGIN_SESSION_VERIFY_FAILED")
        user_email = str((body.get("user") or {}).get("email") or "").strip().lower()
        if user_email and user_email != email.lower():
            raise RuntimeError("MZJ_SESSION_ACCOUNT_MISMATCH")
        return s
    except Exception:
        s.close()
        raise


def get_checkin_state(s: requests.Session) -> dict:
    response, body = request_json(s, "GET", "/api/rewards/daily-checkin")
    if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("code", response.status_code) if isinstance(body, dict) else response.status_code
        raise RuntimeError(f"MZJ_CHECKIN_STATE_FAILED [{code}]")
    return {"claimed_today": bool(body.get("claimedToday")),
            "tokens": body.get("tokens"), "expiry_days": body.get("expiryDays")}


def claim(s: requests.Session) -> dict:
    response, body = request_json(s, "POST", "/api/rewards/daily-checkin")
    if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("code", response.status_code) if isinstance(body, dict) else response.status_code
        raise RuntimeError(f"MZJ_CLAIM_FAILED [{code}]")
    return {"claimed": bool(body.get("claimed")), "tokens": body.get("tokens")}


def run_account(acc: dict, today: str, rows: list[dict], store) -> dict:
    email = acc["email"]
    if SLOT in ("A", "B") and already_done(rows, today, SLOT, email):
        log(f"{email}: 槽位 {SLOT} 今日已成功，跳过")
        return {"account": email, "success": True, "detail": "槽位已完成", "skipped": True}

    s = ensure_authenticated(email, acc["password"], store.get(email), store)
    try:
        state = get_checkin_state(s)
        if state["claimed_today"]:
            detail = "今日已领取"
            tokens = state.get("tokens")
            claimed = False
            log(f"{email}: 今日已签到，不重复领取")
        else:
            result = claim(s)
            claimed = result["claimed"]
            tokens = result["tokens"]
            detail = "签到成功" if claimed else "已由服务端判定今日已领"
            log(f"{email}: {detail}（{tokens or 0} Token）")
            # Confirm only with status GET; never blindly repeat POST after uncertainty.
            state = get_checkin_state(s)
            if not state["claimed_today"]:
                raise RuntimeError("MZJ_CLAIM_NOT_CONFIRMED")
        return {"account": email, "success": True, "detail": detail,
                "claimed": claimed, "tokens": tokens,
                "expiry_days": state.get("expiry_days")}
    finally:
        s.close()


def main() -> int:
    try:
        accounts = load_accounts()
        seeds = load_seeds()
        from mzj_sessions import CookieStore
        key = (os.getenv("MZJ_SESSION_KEY") or "").strip()
        path = Path(os.getenv("MZJ_SESSION_STATE") or str(STATE_DIR / "mzj_sessions.enc"))
        store = CookieStore(path, key, seeds)
    except (RuntimeError, ImportError) as exc:
        log(str(exc) if isinstance(exc, RuntimeError) else "MZJ_ENCRYPTION_DEPENDENCY_REQUIRED")
        return 2

    wait_for_target()
    today = now_cst().strftime("%Y-%m-%d")
    log(f"开始签到 {BASE} | 账号 {len(accounts)} | 日期 {today} | 槽位 {SLOT}")
    rows = history_rows()
    results = []
    for account in accounts:
        try:
            result = run_account(account, today, rows, store)
        except Exception as exc:  # account failures remain isolated
            error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            log(f"{account['email']}: 失败 {error}")
            result = {"account": account["email"], "success": False, "error": error}
        results.append(result)
        if result.get("success") and not result.get("skipped"):
            append_history({
                "ts": now_cst().isoformat(timespec="seconds"),
                "date_cst": today,
                "slot": SLOT,
                "account": result["account"],
                "success": True,
                "detail": result.get("detail"),
                "claimed": result.get("claimed"),
                "tokens": result.get("tokens"),
                "expiry_days": result.get("expiry_days"),
            })
            rows = history_rows()

    ok = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    lines = ["### Mengzhiji 每日签到", "",
             f"- 日期：`{today}`　槽位：`{SLOT}`", f"- 成功：{len(ok)}/{len(results)}",
             "", "| 账号 | 结果 | Token |", "| --- | --- | ---: |"]
    for result in results:
        lines.append(f"| `{result['account']}` | {result.get('detail') or result.get('error') or '失败'} | {result.get('tokens') or '—'} |")
    write_step_summary(lines)
    tg_notify(f"{'✅' if not failed else '⚠️'} Mengzhiji {today} 槽位{SLOT} {len(ok)}/{len(results)}")
    if failed:
        log(f"失败账号数：{len(failed)}")
        return 1
    log(f"全部账号完成：{len(ok)}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())