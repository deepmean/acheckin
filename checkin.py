#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentRouter 每日自动签到（登录即签到）
站点: https://agentrouter.org

原理（2026-10 实测）:
  1. 该站没有独立的签到路由：POST /api/user/checkin -> 404 (网关 Invalid URL)
  2. 每日首次登录时服务端自动完成签到，并在登录响应里返回 checked_in 字段
     前端逻辑: data.checked_in ? "签到成功，新增额度已到账" : "登录成功！"
  3. 登录拿到的 session Cookie 需要配合请求头 New-Api-User: <用户id>
     才能读取 /api/user/self（额度、已用额度）

流程:
  登录 -> 校验 success -> 读 /api/user/self 额度快照 -> 写 history.jsonl -> 汇总

环境变量:
  AGENTROUTER_USERNAME   必填，登录账号（邮箱）
  AGENTROUTER_PASSWORD   必填，登录密码
  AGENTROUTER_BASE       可选，默认 https://agentrouter.org
  BACKUP_RUN             可选，'1' 表示这是当天备份批次：若当天已签到成功则直接跳过
  TG_BOT_TOKEN/TG_CHAT_ID 可选，签到结果推送到 Telegram
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = (os.getenv("AGENTROUTER_BASE") or "https://agentrouter.org").rstrip("/")
USERNAME = (os.getenv("AGENTROUTER_USERNAME") or "").strip()
PASSWORD = os.getenv("AGENTROUTER_PASSWORD") or ""
BACKUP_RUN = (os.getenv("BACKUP_RUN") or "").strip().lower() not in ("", "0", "false", "no")
TG_TOKEN = (os.getenv("TG_BOT_TOKEN") or "").strip()
TG_CHAT = (os.getenv("TG_CHAT_ID") or "").strip()

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HISTORY = STATE_DIR / "history.jsonl"

CST = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

QUOTA_PER_UNIT = 500_000.0  # New API 默认 1 USD = 500000 quota


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] {msg}", flush=True)


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": BASE,
        "Referer": f"{BASE}/login",
        "Connection": "keep-alive",
    })
    return s


def login(s: requests.Session, attempts: int = 3) -> dict:
    """登录 = 签到。返回登录响应 data 段。"""
    url = f"{BASE}/api/user/login?turnstile="
    payload = {"username": USERNAME, "password": PASSWORD}
    last_err = "未知错误"
    for i in range(1, attempts + 1):
        try:
            r = s.post(url, json=payload, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log(f"第 {i}/{attempts} 次尝试被限流/服务端错误: {last_err}")
            else:
                try:
                    body = r.json()
                except ValueError:
                    last_err = f"HTTP {r.status_code} 非 JSON 响应: {r.text[:200]}"
                    log(f"第 {i}/{attempts} 次尝试失败: {last_err}")
                else:
                    if not body.get("success"):
                        # 账号密码错误之类的确定性失败，不必重试
                        raise RuntimeError(f"登录失败: {body.get('message') or body}")
                    data = body.get("data") or {}
                    if not isinstance(data, dict) or not data.get("id"):
                        raise RuntimeError(f"登录响应异常，未拿到用户信息: {str(body)[:300]}")
                    return data
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"第 {i}/{attempts} 次尝试网络异常: {last_err}")
        if i < attempts:
            time.sleep(5 * i)
    raise RuntimeError(f"登录重试 {attempts} 次仍失败: {last_err}")


def fetch_self(s: requests.Session, uid: str) -> dict:
    """读取用户详情（额度快照）。失败不影响签到结论。"""
    s.headers["New-Api-User"] = str(uid)
    try:
        r = s.get(f"{BASE}/api/user/self", timeout=25)
        body = r.json()
        return body.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"读取 /api/user/self 失败（不影响签到）: {type(exc).__name__}: {exc}")
        return {}


def read_history() -> list[dict]:
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


def already_ok_today(today: str) -> bool:
    return any(r.get("date_cst") == today and r.get("success") for r in read_history())


def append_history(rec: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def quota_str(q) -> str:
    try:
        q = int(q or 0)
    except (TypeError, ValueError):
        return str(q)
    return f"{q:,} (约 ${q / QUOTA_PER_UNIT:,.2f})"


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
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"Telegram 推送失败: {type(exc).__name__}: {exc}")


def main() -> int:
    if not USERNAME or not PASSWORD:
        log("缺少 AGENTROUTER_USERNAME / AGENTROUTER_PASSWORD 环境变量")
        return 2

    today = now_cst().strftime("%Y-%m-%d")
    if BACKUP_RUN and already_ok_today(today):
        log(f"{today} 已签到成功，备份批次跳过")
        write_step_summary([f"### AgentRouter 签到 `{today}`", "", "备份批次：今日已成功，跳过。"])
        return 0

    log(f"开始签到处 {BASE}，账号 {USERNAME}，日期 {today}（北京时间）")
    s = new_session()
    try:
        data = login(s)
    except RuntimeError as exc:
        log(str(exc))
        append_history({"date_cst": today, "ts": now_cst().isoformat(timespec="seconds"),
                        "success": False, "checked_in": None, "error": str(exc)})
        write_step_summary([f"### AgentRouter 签到 `{today}` 失败", "", f"```\n{exc}\n```"])
        tg_notify(f"❌ AgentRouter 签到失败 {today}\n{exc}")
        return 1

    uid = str(data.get("id"))
    username = data.get("username") or ""
    checked_in = bool(data.get("checked_in"))
    self_info = fetch_self(s, uid)
    quota = self_info.get("quota", data.get("quota"))
    used = self_info.get("used_quota", data.get("used_quota"))
    group = self_info.get("group") or data.get("group")

    verdict = "签到成功，新增额度已到账" if checked_in else "登录成功（今日签到状态由服务端判定）"
    log(f"登录成功: id={uid} username={username} group={group}")
    log(f"checked_in={checked_in} -> {verdict}")
    log(f"当前额度: {quota_str(quota)}  已用: {quota_str(used)}")

    rec = {
        "date_cst": today,
        "ts": now_cst().isoformat(timespec="seconds"),
        "success": True,
        "checked_in": checked_in,
        "verdict": verdict,
        "user_id": uid,
        "username": username,
        "group": group,
        "quota": quota,
        "used_quota": used,
    }
    append_history(rec)
    write_step_summary([
        f"### AgentRouter 签到 `{today}`",
        "",
        f"- 结论：**{verdict}**（`checked_in={checked_in}`）",
        f"- 账号：`{username}`（id `{uid}`，分组 `{group}`）",
        f"- 额度：{quota_str(quota)}",
        f"- 已用：{quota_str(used)}",
        f"- 时间：{rec['ts']}",
    ])
    tg_notify(f"✅ AgentRouter {today} {verdict}\n账号 {username}\n额度 {quota_str(quota)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
