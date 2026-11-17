#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DGB check-in using an existing refresh session only.

DGB_ENABLED must explicitly be true. DGB_SESSIONS supplies existing
new_api_refresh cookies; DGB_SESSION_KEY encrypts rotated cookies in
DGB_SESSION_STATE. Password login and automatic logout are intentionally absent.
Missing, expired or uncertain sessions fail closed without creating a session.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

BASE = (os.getenv("DGB_BASE") or "https://freeapi.dgbmc.top").rstrip("/")
SLOT = (os.getenv("SLOT") or "manual").strip() or "manual"
SLOT_TARGET = (os.getenv("SLOT_TARGET") or "").strip()
TG_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.getenv("TG_CHAT_ID") or "").strip()

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HISTORY = STATE_DIR / "history_dgb.jsonl"

CST = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
QUOTA_PER_UNIT = 500_000.0


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] [dgb] {msg}", flush=True)


def quota_str(q) -> str:
    try:
        q = int(q or 0)
    except (TypeError, ValueError):
        return str(q)
    return f"{q:,} (约 ${q / QUOTA_PER_UNIT:,.2f})"


# ------------------------------- 账号来源 ------------------------------- #
def load_accounts() -> list[dict]:
    """Read account names only; legacy password fields are ignored."""
    accounts: list[dict] = []
    seen: set[str] = set()

    def push(user) -> None:
        if not isinstance(user, str):
            return
        user = user.strip()
        if user and user not in seen:
            seen.add(user)
            accounts.append({"username": user})

    raw = (os.getenv("DGB_ACCOUNTS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if not isinstance(parsed, list):
                raise ValueError("invalid accounts")
            for item in parsed:
                if isinstance(item, dict):
                    push(item.get("username") or item.get("user"))
                elif isinstance(item, str):
                    push(item.split(":", 1)[0])
        except (ValueError, TypeError):
            raise RuntimeError("DGB_ACCOUNTS_INVALID") from None
    push(os.getenv("DGB_USERNAME"))
    push(os.getenv("DGB_USERNAME_2"))
    return accounts


# --------------------------------- 历史 --------------------------------- #
def history_rows() -> list[dict]:
    if not HISTORY.exists():
        return []
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def already_done(rows: list[dict], today: str, slot: str, account: str) -> bool:
    for r in rows:
        if r.get("date_cst") != today or not r.get("success") or r.get("slot") != slot:
            continue
        if str(r.get("account") or "") == account:
            return True
    return False


def append_history(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------- 通知 --------------------------------- #
def write_step_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def tg_notify(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text,
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"Telegram 推送失败: {type(exc).__name__}: {exc}")


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


# -------------------------------- HTTP --------------------------------- #
def new_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/login",
    })
    return s


def refresh_session(s: requests.Session, store, user: str) -> str:
    entry = store.begin_refresh(user, os.getenv("DGB_CHECKPOINT_ID") or "")
    origin = urlsplit(BASE)
    if origin.scheme != "https" and origin.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("DGB_HTTPS_REQUIRED")
    s.cookies.set("new_api_refresh", entry["refresh_cookie"],
                  domain=origin.hostname, path="/api/user/auth",
                  secure=origin.scheme == "https")
    headers = {"X-Auth-Session": entry["sid"]} if entry.get("sid") else {}
    try:
        response = s.post(BASE + "/api/user/auth/refresh", headers=headers, timeout=30,
                          allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise RuntimeError("DGB_REFRESH_REDIRECT_REJECTED")
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is not True:
            code = body.get("code", "REFRESH_FAILED") if isinstance(body, dict) else "REFRESH_FAILED"
            code = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else "REFRESH_FAILED"
            raise RuntimeError(f"DGB_REFRESH_REJECTED [{code}]")
        data = body.get("data") or {}
        token = data.get("access_token")
        sid = (data.get("session") or {}).get("sid")
        if (data.get("user") or {}).get("username") != user:
            raise RuntimeError("DGB_SESSION_ACCOUNT_MISMATCH")
        if not isinstance(token, str) or not token or not isinstance(sid, str) or not sid:
            raise RuntimeError("DGB_REFRESH_RESPONSE_INVALID")
        if entry.get("sid") and entry["sid"] != sid:
            raise RuntimeError("DGB_SESSION_ID_MISMATCH")
        cookie = response.cookies.get("new_api_refresh")
        store.rotated(user, cookie, sid)
        return token
    except requests.RequestException:
        raise RuntimeError("DGB_REFRESH_TRANSPORT_FAILED") from None
    except (ValueError, AttributeError, TypeError):
        raise RuntimeError("DGB_REFRESH_RESPONSE_INVALID") from None


def get_json(s: requests.Session, token: str, path: str, timeout: int = 30) -> dict:
    try:
        r = s.get(BASE + path, headers={"Authorization": f"Bearer {token}"},
                  timeout=timeout, allow_redirects=False)
        if 300 <= r.status_code < 400:
            raise RuntimeError("DGB_REQUEST_REDIRECT_REJECTED")
        return r.json() or {}
    except Exception as exc:  # noqa: BLE001
        log(f"GET {path} 失败：{type(exc).__name__}: {exc}")
        return {}


def do_checkin(s: requests.Session, token: str) -> tuple[bool, str, dict]:
    """执行签到。返回 (是否成功, 说明, 原始响应)。"""
    r = s.post(f"{BASE}/api/user/checkin",
               headers={"Authorization": f"Bearer {token}"}, timeout=40,
               allow_redirects=False)
    if 300 <= r.status_code < 400:
        return False, "DGB_REQUEST_REDIRECT_REJECTED", {}
    try:
        body = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code} 非 JSON: {r.text[:200]}", {}

    if body.get("success"):
        d = body.get("data") or {}
        awarded = d.get("quota_awarded") or d.get("quota") or d.get("reward")
        if awarded:
            return True, f"签到成功，获得 {quota_str(awarded)}", body
        return True, "签到成功", body

    key = str(body.get("message_key") or "")
    msg = str(body.get("message") or "")
    if key == "Already checked in today" or "already checked in" in msg.lower() \
            or "已签到" in msg or "已经签到" in msg:
        return True, "今天已签到（无需重复）", body
    return False, f"签到失败: {msg or str(body)[:200]}", body


# ------------------------------- 单账号流程 ------------------------------- #
def run_account(acc: dict, today: str, rows: list[dict], store) -> dict:
    user = acc["username"]
    if SLOT in ("A", "B") and already_done(rows, today, SLOT, user):
        log(f"{user}: 槽位 {SLOT} 今天已成功，跳过")
        return {"account": user, "success": True, "detail": "槽位已完成", "skipped": True}

    # Check before opening a connection; an absent or quarantined cookie must
    # never trigger password login or fall back to a stale seed.
    store.get(user, os.getenv("DGB_CHECKPOINT_ID") or "")
    s = new_session()
    try:
        token = refresh_session(s, store, user)
        log(f"{user}: 已复用并保存原有会话")
        return check_authenticated(s, token, user, today)
    finally:
        s.close()


def check_authenticated(s: requests.Session, token: str, user: str, today: str) -> dict:
    stats = (get_json(s, token, "/api/user/checkin").get("data") or {})
    st = stats.get("stats") or {}

    before = (get_json(s, token, "/api/user/self").get("data") or {}).get("quota")
    if st.get("checked_in_today"):
        ok, detail, body = True, "今天已签到（服务端状态）", {}
        log(f"{user}: 服务端显示今天已签到，跳过 POST。累计 {st.get('checkin_count')} 次")
    else:
        ok, detail, body = do_checkin(s, token)
        log(f"{user}: {detail}")

    stats2 = (get_json(s, token, "/api/user/checkin").get("data") or {})
    st2 = stats2.get("stats") or {}
    after = (get_json(s, token, "/api/user/self").get("data") or {}).get("quota")

    awarded = None
    if before is not None and after is not None:
        try:
            awarded = int(after) - int(before)
        except (TypeError, ValueError):
            awarded = None
    if not awarded:
        recs = st2.get("records") or []
        if recs and recs[0].get("checkin_date") == today:
            awarded = recs[0].get("quota_awarded")
    if not awarded and isinstance(body, dict):
        d = body.get("data")
        if isinstance(d, dict):
            awarded = d.get("quota_awarded") or d.get("quota")

    return {
        "account": user,
        "success": bool(ok),
        "detail": detail,
        "awarded": awarded,
        "checked_in_today": bool(st2.get("checked_in_today")),
        "checkin_count": st2.get("checkin_count"),
        "total_quota": st2.get("total_quota"),
        "quota_after": after,
    }


def main() -> int:
    if (os.getenv("DGB_ENABLED") or "").strip().lower() != "true":
        log("DGB 自动签到已停用；未发起任何认证请求")
        return 0

    try:
        from dgb_sessions import SessionStore
        path = Path(os.getenv("DGB_SESSION_STATE") or str(STATE_DIR / "dgb_sessions.enc"))
        store = SessionStore(path, os.getenv("DGB_SESSION_KEY") or "", BASE,
                             os.getenv("DGB_SESSIONS") or "")
        accounts = load_accounts() or [{"username": user} for user in store.users()]
        if not accounts:
            raise RuntimeError("DGB_SESSION_REQUIRED")
    except (RuntimeError, ImportError) as exc:
        log(str(exc) if isinstance(exc, RuntimeError) else "DGB_ENCRYPTION_DEPENDENCY_REQUIRED")
        return 2

    if "--prepare" in sys.argv[1:]:
        try:
            checkpoint = os.getenv("DGB_CHECKPOINT_ID") or ""
            today = now_cst().strftime("%Y-%m-%d")
            rows = history_rows()
            pending = [acc["username"] for acc in accounts
                       if SLOT not in ("A", "B") or not already_done(rows, today, SLOT, acc["username"])]
            errors = store.prepare(pending, checkpoint)
            for user, error in errors.items():
                log(f"{user}: {error}")
            log("已准备加密会话检查点；远端保存成功后才允许刷新")
            return 0
        except RuntimeError as exc:
            log(str(exc))
            return 2

    wait_for_target()
    today = now_cst().strftime("%Y-%m-%d")
    log(f"开始签到 {BASE} | 账号数 {len(accounts)} | 日期 {today} | 槽位 {SLOT}")

    rows = history_rows()
    results: list[dict] = []
    for acc in accounts:
        try:
            res = run_account(acc, today, rows, store)
        except Exception as exc:  # noqa: BLE001
            log(f"{acc['username']}: 未预期异常 {type(exc).__name__}: {exc}")
            res = {"account": acc["username"], "success": False,
                   "error": f"{type(exc).__name__}: {exc}"}
        results.append(res)

        if not res.get("skipped"):
            rec = {
                "date_cst": today,
                "ts": now_cst().isoformat(timespec="seconds"),
                "slot": SLOT,
                "account": res["account"],
                "success": bool(res.get("success")),
                "detail": res.get("detail") or res.get("error"),
                "checked_in_today": res.get("checked_in_today"),
                "awarded": res.get("awarded"),
                "checkin_count": res.get("checkin_count"),
                "total_quota": res.get("total_quota"),
                "quota_after": res.get("quota_after"),
            }
            append_history(rec)

    ok_count = sum(1 for r in results if r.get("success"))
    failed = [r for r in results if not r.get("success")]

    lines = [f"### DGB 签到 `{today}`（槽位 {SLOT}）", "",
             f"账号 {ok_count}/{len(results)} 成功", "",
             "| 账号 | 结果 | 本次奖励 | 当前额度 | 累计 |",
             "| --- | --- | --- | --- | --- |"]
    for r in results:
        lines.append("| `{}` | {} | {} | {} | {} 次 |".format(
            r["account"],
            r.get("detail") or r.get("error") or "—",
            quota_str(r["awarded"]) if r.get("awarded") else "—",
            quota_str(r["quota_after"]) if r.get("quota_after") is not None else "—",
            r.get("checkin_count") or "—",
        ))
    write_step_summary(lines)

    summary = "；".join(f"{r['account']}={'OK' if r.get('success') else 'FAIL'}" for r in results)
    tg_notify(f"{'✅' if not failed else '⚠️'} DGB {today} 槽位{SLOT} {ok_count}/{len(results)}\n{summary}")

    if failed:
        for r in failed:
            log(f"失败：{r['account']} -> {r.get('detail') or r.get('error')}")
        return 1
    log(f"全部账号签到完成：{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
