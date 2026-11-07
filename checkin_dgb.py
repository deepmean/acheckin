#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DGB 公益站（freeapi.dgbmc.top）每日自动签到 —— 多账号版
站点: https://freeapi.dgbmc.top   （New API v1.0.0-rc.47）

一、站点机制（2026-11 实测）
  1. 登录：POST /api/user/login?turnstile=
     返回 data.access_token（Bearer）+ data.session + data.user
     与 AgentRouter 不同，这里没有 New-Api-User 头，改用 Authorization: Bearer <token>
  2. 查询：GET /api/user/checkin
     {"data":{"enabled":true,"max_quota":25555000,"min_quota":15000000,
              "stats":{"checked_in_today":true,"checkin_count":1,
                       "records":[{"checkin_date":"2026-11-07","quota_awarded":21740906}],
                       "total_checkins":1,"total_quota":21740906}}}
  3. 签到：POST /api/user/checkin
     成功 → success=true，奖励在 min_quota ~ max_quota 之间随机
     重复 → HTTP 200 + {"message":"Already checked in today","success":false}
            （这是「今天已签」，不是失败）

二、多账号
  账号来源（优先级从高到低）：
    · DGB_ACCOUNTS  —— JSON 数组，如
        [{"username":"a","password":"p1"},{"username":"b","password":"p2"}]
    · DGB_USERNAME / DGB_PASSWORD          （第 1 个账号）
    · DGB_USERNAME_2 / DGB_PASSWORD_2      （第 2 个账号）
  每个账号独立登录、独立签到、独立记录；某个账号失败不影响其它账号。

三、调度
  与 AgentRouter 完全同一套规则：每天两个随机时间、严格间隔 12 小时、分钟避开整点。
  workflow 的 gate.py 统一算槽位，把 SLOT / SLOT_TARGET 传进来；
  本脚本按「账号 + 槽位」去重，同一账号当天同一槽位不会重复签。

环境变量
  DGB_ACCOUNTS / DGB_USERNAME(_2) / DGB_PASSWORD(_2)  见上
  DGB_BASE       可选，默认 https://freeapi.dgbmc.top
  SLOT           可选，A / B / manual
  SLOT_TARGET    可选，目标时间（ISO），未到点则先等
  TG_BOT_TOKEN/TG_CHAT_ID 可选，Telegram 推送
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

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
    """返回 [{'username':..,'password':..}, ...]，顺序稳定、去重。"""
    accounts: list[dict] = []
    seen: set[str] = set()

    def push(user: str | None, pwd: str | None) -> None:
        u = (user or "").strip()
        p = pwd or ""
        if not u or not p or u in seen:
            return
        seen.add(u)
        accounts.append({"username": u, "password": p})

    raw = (os.getenv("DGB_ACCOUNTS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            for item in parsed if isinstance(parsed, list) else []:
                if isinstance(item, dict):
                    push(item.get("username") or item.get("user"), item.get("password") or item.get("pass"))
                elif isinstance(item, str) and ":" in item:
                    u, _, p = item.partition(":")
                    push(u, p)
        except ValueError as exc:
            log(f"DGB_ACCOUNTS 解析失败（{exc}），回退到 DGB_USERNAME* 变量")

    push(os.getenv("DGB_USERNAME"), os.getenv("DGB_PASSWORD"))
    push(os.getenv("DGB_USERNAME_2"), os.getenv("DGB_PASSWORD_2"))
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


def login(s: requests.Session, user: str, pwd: str, attempts: int = 3) -> str:
    last = "未知错误"
    for i in range(1, attempts + 1):
        try:
            r = s.post(f"{BASE}/api/user/login?turnstile=",
                       json={"username": user, "password": pwd}, timeout=40)
            body = r.json()
            if body.get("success") and isinstance(body.get("data"), dict):
                token = body["data"].get("access_token")
                if token:
                    return token
                last = f"登录成功但无 access_token: {str(body)[:160]}"
            else:
                last = f"登录被拒: {body.get('message') or body}"
                low = str(last).lower()
                if "密码" in last or "password" in low or "credential" in low or "invalid" in low:
                    raise RuntimeError(last)   # 凭据错误不重试
            log(f"  {user} 第 {i}/{attempts} 次登录失败：{last}")
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            log(f"  {user} 第 {i}/{attempts} 次网络异常：{last}")
        except ValueError:
            last = f"HTTP {r.status_code} 非 JSON: {r.text[:160]}"
            log(f"  {user} 第 {i}/{attempts} 次：{last}")
        if i < attempts:
            time.sleep(4 * i)
    raise RuntimeError(f"登录重试 {attempts} 次仍失败: {last}")


def get_json(s: requests.Session, token: str, path: str, timeout: int = 30) -> dict:
    try:
        r = s.get(BASE + path, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        return r.json() or {}
    except Exception as exc:  # noqa: BLE001
        log(f"GET {path} 失败：{type(exc).__name__}: {exc}")
        return {}


def do_checkin(s: requests.Session, token: str) -> tuple[bool, str, dict]:
    """执行签到。返回 (是否成功, 说明, 原始响应)。"""
    r = s.post(f"{BASE}/api/user/checkin",
               headers={"Authorization": f"Bearer {token}"}, timeout=40)
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
def run_account(acc: dict, today: str, rows: list[dict]) -> dict:
    user = acc["username"]
    if SLOT in ("A", "B") and already_done(rows, today, SLOT, user):
        log(f"{user}: 槽位 {SLOT} 今天已成功，跳过")
        return {"account": user, "success": True, "detail": "槽位已完成", "skipped": True}

    s = new_session()
    try:
        token = login(s, user, acc["password"])
    except RuntimeError as exc:
        log(f"{user}: {exc}")
        return {"account": user, "success": False, "error": str(exc)}

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
    accounts = load_accounts()
    if not accounts:
        log("没有可用账号（DGB_ACCOUNTS / DGB_USERNAME* 都为空）")
        return 2

    wait_for_target()
    today = now_cst().strftime("%Y-%m-%d")
    log(f"开始签到 {BASE} | 账号数 {len(accounts)} | 日期 {today} | 槽位 {SLOT}")

    rows = history_rows()
    results: list[dict] = []
    for acc in accounts:
        try:
            res = run_account(acc, today, rows)
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
