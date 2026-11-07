#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DGB 公益站（freeapi.dgbmc.top）每日自动签到
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

二、调度
  与 AgentRouter 完全同一套规则：每天两个随机时间、严格间隔 12 小时、分钟避开整点。
  由 workflow 里的 gate.py 统一算槽位，把 SLOT / SLOT_TARGET 传给本脚本；
  本脚本自己再读 state/history_dgb.jsonl 去重，同一槽位当天不重复签。

环境变量
  DGB_USERNAME   必填
  DGB_PASSWORD   必填
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
USERNAME = (os.getenv("DGB_USERNAME") or "").strip()
PASSWORD = os.getenv("DGB_PASSWORD") or ""
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


def already_done(today: str, slot: str) -> bool:
    for r in history_rows():
        if r.get("date_cst") == today and r.get("success") and r.get("slot") == slot:
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


def login(s: requests.Session, attempts: int = 3) -> str:
    last = "未知错误"
    for i in range(1, attempts + 1):
        try:
            r = s.post(f"{BASE}/api/user/login?turnstile=",
                       json={"username": USERNAME, "password": PASSWORD}, timeout=40)
            body = r.json()
            if body.get("success") and isinstance(body.get("data"), dict):
                token = body["data"].get("access_token")
                if token:
                    return token
                last = f"登录成功但无 access_token: {str(body)[:200]}"
            else:
                last = f"登录被拒: {body.get('message') or body}"
                if "密码" in str(last) or "password" in str(last).lower():
                    raise RuntimeError(last)
            log(f"第 {i}/{attempts} 次登录失败：{last}")
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            log(f"第 {i}/{attempts} 次网络异常：{last}")
        except ValueError:
            last = f"HTTP {r.status_code} 非 JSON: {r.text[:160]}"
            log(f"第 {i}/{attempts} 次：{last}")
        if i < attempts:
            time.sleep(4 * i)
    raise RuntimeError(f"登录重试 {attempts} 次仍失败: {last}")


def get_checkin_stats(s: requests.Session, token: str) -> dict:
    try:
        r = s.get(f"{BASE}/api/user/checkin",
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return (r.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"读取签到状态失败（不影响后续）：{type(exc).__name__}: {exc}")
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


def main() -> int:
    if not USERNAME or not PASSWORD:
        log("缺少 DGB_USERNAME / DGB_PASSWORD")
        return 2

    wait_for_target()
    today = now_cst().strftime("%Y-%m-%d")

    if SLOT in ("A", "B") and already_done(today, SLOT):
        log(f"槽位 {SLOT} 今天已成功，跳过")
        write_step_summary([f"### DGB 签到 `{today}`（槽位 {SLOT}）", "", "今天该槽位已完成，跳过。"])
        return 0

    log(f"开始签到 {BASE} | 账号 {USERNAME} | 日期 {today} | 槽位 {SLOT}")

    s = new_session()
    try:
        token = login(s)
    except RuntimeError as exc:
        err = str(exc)
        log(err)
        append_history({"date_cst": today, "ts": now_cst().isoformat(timespec="seconds"),
                        "slot": SLOT, "success": False, "error": err[:400]})
        write_step_summary([f"### DGB 签到 `{today}`（槽位 {SLOT}）失败", "", f"```\n{err}\n```"])
        tg_notify(f"❌ DGB 签到失败 {today} 槽位{SLOT}\n{err[:300]}")
        return 1

    stats = get_checkin_stats(s, token)
    st = stats.get("stats") or {}
    before_quota = None
    try:
        r0 = s.get(f"{BASE}/api/user/self",
                   headers={"Authorization": f"Bearer {token}"}, timeout=25)
        before_quota = ((r0.json() or {}).get("data") or {}).get("quota")
    except Exception:  # noqa: BLE001
        pass

    if st.get("checked_in_today"):
        ok, detail, body = True, "今天已签到（服务端状态）", {}
        log(f"服务端显示今天已签到，跳过 POST。累计 {st.get('checkin_count')} 次，"
            f"累计奖励 {quota_str(st.get('total_quota'))}")
    else:
        ok, detail, body = do_checkin(s, token)
        log(detail)

    stats2 = get_checkin_stats(s, token)
    st2 = stats2.get("stats") or {}
    after_quota = None
    try:
        r1 = s.get(f"{BASE}/api/user/self",
                   headers={"Authorization": f"Bearer {token}"}, timeout=25)
        after_quota = ((r1.json() or {}).get("data") or {}).get("quota")
    except Exception:  # noqa: BLE001
        pass

    awarded = None
    if before_quota is not None and after_quota is not None:
        try:
            awarded = int(after_quota) - int(before_quota)
        except (TypeError, ValueError):
            awarded = None
    if not awarded:
        recs = st2.get("records") or []
        if recs and recs[-1].get("checkin_date") == today:
            awarded = recs[-1].get("quota_awarded")
    if not awarded:
        d = body.get("data") if isinstance(body, dict) else None
        if isinstance(d, dict):
            awarded = d.get("quota_awarded") or d.get("quota")

    rec = {
        "date_cst": today,
        "ts": now_cst().isoformat(timespec="seconds"),
        "slot": SLOT,
        "success": bool(ok),
        "detail": detail,
        "checked_in_today": bool(st2.get("checked_in_today")),
        "awarded": awarded,
        "checkin_count": st2.get("checkin_count"),
        "total_quota": st2.get("total_quota"),
        "quota_after": after_quota,
        "user_id": stats.get("user_id"),
    }
    append_history(rec)
    write_step_summary([
        f"### DGB 签到 `{today}`（槽位 {SLOT}）", "",
        f"- 结论：**{detail}**",
        f"- 今日是否已签：`{st2.get('checked_in_today')}`",
        f"- 本次奖励：{quota_str(awarded) if awarded else '—'}",
        f"- 当前额度：{quota_str(after_quota) if after_quota is not None else '—'}",
        f"- 累计签到：{st2.get('checkin_count')} 次 / {quota_str(st2.get('total_quota'))}",
        f"- 时间：{rec['ts']}",
    ])
    tg_notify(f"{'✅' if ok else '❌'} DGB {today} 槽位{SLOT} {detail}\n"
              f"奖励 {quota_str(awarded) if awarded else '—'}\n"
              f"额度 {quota_str(after_quota) if after_quota is not None else '—'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
