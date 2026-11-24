#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RunningHub daily points check-in — refresh token only, never passwords.

Measured on 2026-11-24:
  POST /uc/abroad/pwdLogin {email, password: md5_hex}
      -> data.access_token, data.refresh_token, expire_in 2592000000 (30 days)
  POST /uc/token/refresh   Authorization: Bearer <refresh_token>
      -> new access/refresh pair; the previously issued refresh token keeps
         working (verified: it still refreshed after a rotation), so no
         rotation state has to survive between runs.
  POST /api/welfare/summary          -> data.dailyGift.granted
  POST /api/welfare/daily-gift/claim -> data True when granted now,
                                        False when already granted today
                                        (server-side idempotent: balance unchanged)
  Reward 30 POINT per day, reset at 00:00 Asia/Shanghai.

Design: credentials are never mounted. Each account carries an existing
refresh token in RH_ACCOUNTS; a missing or rejected token fails that account
only, and there is no password fallback anywhere in this file.

Environment
  RH_ACCOUNTS   JSON array [{"email": ..., "refresh_token": ...}, ...]
  RH_BASE       optional, default https://www.runninghub.ai
  SLOT          A / B / manual       (from gate.py)
  SLOT_TARGET   optional ISO target time
  TG_BOT_TOKEN / TG_CHAT_ID          optional Telegram push
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = (os.getenv("RH_BASE") or "https://www.runninghub.ai").rstrip("/")
SLOT = (os.getenv("SLOT") or "manual").strip() or "manual"
SLOT_TARGET = (os.getenv("SLOT_TARGET") or "").strip()
TG_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.getenv("TG_CHAT_ID") or "").strip()

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HISTORY = STATE_DIR / "history_rh.jsonl"

CST = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
RETRIES = 3


# ------------------------------- 基础工具 ------------------------------- #
def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] [rh] {msg}", flush=True)


def load_accounts() -> list[dict]:
    """Parse RH_ACCOUNTS; any malformed value fails closed (no requests)."""
    raw = (os.getenv("RH_ACCOUNTS") or "").strip()
    if not raw:
        raise RuntimeError("RH_ACCOUNTS 为空：请导入 refresh_token")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise RuntimeError("RH_ACCOUNTS 不是合法 JSON") from None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("RH_ACCOUNTS 必须是账号数组")

    accounts: list[dict] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            raise RuntimeError("RH_ACCOUNTS 含非对象条目")
        email = (item.get("email") or item.get("username") or "").strip()
        token = (item.get("refresh_token") or item.get("refreshToken") or "").strip()
        if not email or not token:
            raise RuntimeError("RH_ACCOUNTS 条目缺少 email 或 refresh_token")
        if email in seen:
            continue
        seen.add(email)
        accounts.append({"email": email, "refresh_token": token})
    return accounts


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


def already_done(rows: list[dict], today: str, slot: str, email: str) -> bool:
    for r in rows:
        if r.get("date_cst") != today or not r.get("success") or r.get("slot") != slot:
            continue
        if str(r.get("account") or "") == email:
            return True
    return False


def append_history(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


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
        "Referer": BASE + "/",
    })
    return s


def post_json(s: requests.Session, path: str, token: str = "", body: dict | None = None) -> dict:
    """POST with bounded retries. Redirects are refused so a 3xx can never be
    silently replayed against the login endpoint."""
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            r = s.post(BASE + path, json=body or {}, headers=headers,
                       timeout=30, allow_redirects=False)
            if 300 <= r.status_code < 400:
                raise RuntimeError(f"{path} 返回重定向 HTTP {r.status_code}")
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"{path} 返回非对象 JSON")
            return data
        except (requests.RequestException, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            log(f"  {path} 第 {attempt}/{RETRIES} 次失败：{last}")
            if attempt < RETRIES:
                time.sleep(2 * attempt)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from None
    raise RuntimeError(f"{path} 重试 {RETRIES} 次仍失败：{last}")


def access_token_from(s: requests.Session, refresh_token: str) -> str:
    body = post_json(s, "/uc/token/refresh", token=refresh_token)
    if body.get("code") != 0:
        raise RuntimeError(f"REFRESH_REJECTED [{body.get('code')}] {body.get('msg') or ''}".strip())
    data = body.get("data") or {}
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("刷新成功但未返回 access_token")
    return access


def gift_granted(s: requests.Session, access: str) -> dict:
    body = post_json(s, "/api/welfare/summary", token=access)
    if body.get("code") != 0:
        raise RuntimeError(f"SUMMARY_FAILED [{body.get('code')}] {body.get('msg') or ''}".strip())
    gift = ((body.get("data") or {}).get("dailyGift") or {})
    return {"granted": bool(gift.get("granted")),
            "amount": gift.get("amount"), "expire": gift.get("expireTime")}


def claim_gift(s: requests.Session, access: str) -> bool:
    body = post_json(s, "/api/welfare/daily-gift/claim", token=access)
    if body.get("code") != 0:
        raise RuntimeError(f"CLAIM_FAILED [{body.get('code')}] {body.get('msg') or ''}".strip())
    return bool(body.get("data"))


def point_balance(s: requests.Session, access: str):
    try:
        body = post_json(s, "/uc/point/summary", token=access)
    except RuntimeError:
        return None
    if body.get("code") != 0:
        return None
    return (body.get("data") or {}).get("balancePoint")


# ------------------------------- 单账号 --------------------------------- #
def run_account(acc: dict, today: str, rows: list[dict]) -> dict:
    email = acc["email"]
    if SLOT in ("A", "B") and already_done(rows, today, SLOT, email):
        log(f"{email}: 槽位 {SLOT} 今日已完成，跳过")
        return {"account": email, "success": True, "detail": "槽位已完成", "skipped": True}

    s = new_session()
    try:
        try:
            access = access_token_from(s, acc["refresh_token"])
        except RuntimeError as exc:
            msg = str(exc)
            if "REFRESH_REJECTED" in msg:
                msg += "；请重新导入该账号的 refresh_token"
            log(f"{email}: {msg}")
            return {"account": email, "success": False, "error": msg}

        try:
            state = gift_granted(s, access)
        except RuntimeError as exc:
            log(f"{email}: {exc}")
            return {"account": email, "success": False, "error": str(exc)}

        claimed = False
        if state["granted"]:
            log(f"{email}: 今日已领取（{state['amount']} POINT），不重复提交")
            detail = "今日已领取"
        else:
            try:
                claimed = claim_gift(s, access)
                detail = "领取成功" if claimed else "已领取（服务端返回未新发放）"
            except RuntimeError as exc:
                log(f"{email}: {exc}")
                return {"account": email, "success": False, "error": str(exc)}
            log(f"{email}: {detail}")

        try:
            after = gift_granted(s, access)
        except RuntimeError:
            after = {"granted": state["granted"] or claimed, "amount": state["amount"]}
        if not after["granted"]:
            msg = "领取后仍未标记已领取，请人工确认"
            log(f"{email}: {msg}")
            return {"account": email, "success": False, "error": msg}

        return {"account": email, "success": True, "detail": detail,
                "claimed": claimed, "amount": after.get("amount"),
                "balance": point_balance(s, access)}
    finally:
        s.close()


# --------------------------------- main -------------------------------- #
def main() -> int:
    try:
        accounts = load_accounts()
    except RuntimeError as exc:
        log(str(exc))
        return 2

    wait_for_target()
    today = now_cst().strftime("%Y-%m-%d")
    log(f"开始签到 {BASE} | 账号 {len(accounts)} | 日期 {today} | 槽位 {SLOT}")

    rows = history_rows()
    results = []
    for acc in accounts:
        try:
            res = run_account(acc, today, rows)
        except Exception as exc:  # noqa: BLE001 单个账号异常不拖垮其他账号
            log(f"{acc['email']}: 未预期异常 {type(exc).__name__}")
            res = {"account": acc["email"], "success": False,
                   "error": f"{type(exc).__name__}"}
        results.append(res)
        if res.get("success") and not res.get("skipped"):
            append_history({
                "ts": now_cst().isoformat(timespec="seconds"),
                "date_cst": today,
                "slot": SLOT,
                "account": res["account"],
                "success": True,
                "detail": res.get("detail"),
                "claimed": res.get("claimed"),
                "amount": res.get("amount"),
                "balance": res.get("balance"),
            })
            rows = history_rows()

    ok = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    lines = ["### RunningHub 每日积分", "",
             f"- 槽位：`{SLOT}`　日期：`{today}`",
             f"- 成功 {len(ok)}/{len(results)}",
             "", "| 账号 | 结果 | 积分 |", "| --- | --- | --- |"]
    for r in results:
        lines.append("| `{}` | {} | {} |".format(
            r["account"],
            (r.get("detail") or r.get("error") or "—"),
            r.get("balance") if r.get("balance") is not None else "—"))
    write_step_summary(lines)

    summary = "；".join(f"{r['account']}={'OK' if r.get('success') else 'FAIL'}" for r in results)
    tg_notify(f"{'✅' if not failed else '⚠️'} RunningHub {today} 槽位{SLOT} "
              f"{len(ok)}/{len(results)}\n{summary}")

    if failed:
        for r in failed:
            log(f"失败：{r['account']} -> {r.get('error')}")
        return 1
    log(f"全部账号完成：{len(ok)}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())