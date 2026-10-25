#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentRouter 每日自动签到（登录即签到）
站点: https://agentrouter.org

一、站点签到机制（2026-10 实测）
  1. 没有独立签到路由：POST /api/user/checkin -> 404（网关 Invalid URL）
  2. 每日首次登录即完成签到，登录响应里的 data.checked_in 就是签到结果
     前端原文逻辑：data.checked_in ? "签到成功，新增额度已到账" : "登录成功！"
  3. 登录拿到的 session Cookie 必须配合请求头 New-Api-User: <用户id>
     才能读 /api/user/self（额度、已用额度）

二、为什么会失败（重要）
  agentrouter.org 前置阿里云 WAF，按「出口 IP 信誉」判定：
    - 家庭/办公 IP：直接放行
    - GitHub Actions 默认出口（Azure）：返回 16011 字节的「滑动验证」挑战页
      实测对任意路径、任意 UA、任意请求头、任意方法一律挑战；
      携带真实浏览器的 acw_tc / session cookie 也无效；
      真实 Chrome(Xvfb) 拖滑块被判定「验证失败，请刷新」。
  => WAF 拦截发生在认证之前，换 GitHub OAuth、换 token 登录同样过不去。

  解法：让出口 IP 走 Cloudflare WARP。
    实测 WARP 出口（如 104.28.227.105）不再触发挑战，
    可直接 POST /api/user/login 拿到 success=true、checked_in=true。

三、本脚本通道
  1. 代理通道：CHECKIN_PROXY / HTTPS_PROXY / ALL_PROXY 指向 WARP 本地 socks5
     （workflow 里 warp-cli mode proxy 监听 127.0.0.1:40000）
  2. 直连通道：家庭 IP、自托管 runner、或代理失效时兜底

环境变量
  AGENTROUTER_USERNAME   必填
  AGENTROUTER_PASSWORD   必填
  AGENTROUTER_BASE       可选，默认 https://agentrouter.org
  CHECKIN_PROXY          可选，如 socks5h://127.0.0.1:40000
  BACKUP_RUN             可选 '1'：当天已成功则跳过
  TG_BOT_TOKEN/TG_CHAT_ID 可选，Telegram 推送
  DEBUG_DUMP             可选，打印异常响应
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
DEBUG = bool(os.getenv("DEBUG_DUMP"))

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HISTORY = STATE_DIR / "history.jsonl"

CST = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
QUOTA_PER_UNIT = 500_000.0
WAF_MARKERS = ("aliyun_waf", "访问验证", "Access Verification", "aliyunCaptcha", "滑动验证")


class WafBlocked(RuntimeError):
    pass


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] {msg}", flush=True)


def is_waf_page(text: str) -> bool:
    return any(m in (text or "")[:5000] for m in WAF_MARKERS)


def proxy_candidates() -> list[tuple[str, dict | None]]:
    cands: list[tuple[str, dict | None]] = []
    raw = (os.getenv("CHECKIN_PROXY") or os.getenv("HTTPS_PROXY")
           or os.getenv("https_proxy") or os.getenv("ALL_PROXY") or "").strip()
    if raw:
        cands.append((raw, {"http": raw, "https": raw}))
    cands.append(("direct", None))
    return cands


def new_session(proxies: dict | None) -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 代理只走显式配置，避免环境变量污染
    if proxies:
        s.proxies.update(proxies)
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/login",
        "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    return s


def login_via(label: str, proxies: dict | None, attempts: int = 2) -> dict:
    """登录=签到。成功返回 {data, session, via}；WAF 拦截抛 WafBlocked。"""
    url = f"{BASE}/api/user/login?turnstile="
    payload = {"username": USERNAME, "password": PASSWORD}
    last = "未知错误"
    for i in range(1, attempts + 1):
        s = new_session(proxies)
        try:
            r = s.post(url, json=payload, timeout=45)
            text = r.text or ""
            if is_waf_page(text):
                raise WafBlocked(f"命中阿里云 WAF 挑战页（{len(text)} bytes）")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {text[:150]}"
                log(f"[{label}] 第 {i}/{attempts} 次：{last}")
            else:
                try:
                    body = r.json()
                except ValueError:
                    last = f"HTTP {r.status_code} 非 JSON: {text[:150]}"
                    log(f"[{label}] 第 {i}/{attempts} 次：{last}")
                    if DEBUG:
                        log(f"---- raw ({len(text)}B) ----\n{text[:3000]}\n---- end ----")
                else:
                    if not body.get("success"):
                        raise RuntimeError(f"登录被拒: {body.get('message') or body}")
                    data = body.get("data") or {}
                    if not isinstance(data, dict) or not data.get("id"):
                        raise RuntimeError(f"登录响应异常: {str(body)[:250]}")
                    return {"data": data, "session": s, "via": label}
        except WafBlocked:
            raise
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            log(f"[{label}] 第 {i}/{attempts} 次网络异常：{last}")
        if i < attempts:
            time.sleep(4 * i)
    raise RuntimeError(f"{label} 重试 {attempts} 次仍失败: {last}")


def fetch_self(session: requests.Session, uid: str) -> dict:
    try:
        r = session.get(f"{BASE}/api/user/self",
                        headers={"New-Api-User": str(uid)}, timeout=30)
        return (r.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"读取 /api/user/self 失败（不影响签到结论）：{type(exc).__name__}: {exc}")
        return {}


def quota_str(q) -> str:
    try:
        q = int(q or 0)
    except (TypeError, ValueError):
        return str(q)
    return f"{q:,} (约 ${q / QUOTA_PER_UNIT:,.2f})"


# ------------------------------ 记录 / 通知 ------------------------------ #
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


# ---------------------------------- main ---------------------------------- #
def main() -> int:
    if not USERNAME or not PASSWORD:
        log("缺少 AGENTROUTER_USERNAME / AGENTROUTER_PASSWORD")
        return 2

    today = now_cst().strftime("%Y-%m-%d")
    if BACKUP_RUN and already_ok_today(today):
        log(f"{today} 已签到成功，备份批次跳过")
        write_step_summary([f"### AgentRouter 签到 `{today}`", "", "备份批次：今日已成功，跳过。"])
        return 0

    log(f"开始签到 {BASE} | 账号 {USERNAME} | 日期 {today}")

    result, errors = None, []
    for label, proxies in proxy_candidates():
        try:
            result = login_via(label, proxies)
            log(f"[{label}] 通道成功")
            break
        except WafBlocked as exc:
            errors.append(f"{label}: WAF - {exc}")
            log(f"[{label}] 被 WAF 拦截：{exc}")
        except RuntimeError as exc:
            errors.append(f"{label}: {exc}")
            log(f"[{label}] 失败：{exc}")

    if result is None:
        err = " | ".join(errors) or "未知错误"
        append_history({"date_cst": today, "ts": now_cst().isoformat(timespec="seconds"),
                        "success": False, "checked_in": None, "error": err[:400]})
        write_step_summary([f"### AgentRouter 签到 `{today}` 失败", "",
                            f"```\n{err[:1200]}\n```"])
        tg_notify(f"❌ AgentRouter 签到失败 {today}\n{err[:300]}")
        return 1

    data = result["data"]
    uid = str(data.get("id"))
    username = data.get("username") or ""
    checked_in = bool(data.get("checked_in"))
    info = fetch_self(result["session"], uid)
    quota = info.get("quota", data.get("quota"))
    used = info.get("used_quota", data.get("used_quota"))
    group = info.get("group") or data.get("group")

    verdict = "签到成功，新增额度已到账" if checked_in else "登录成功（今日签到状态以服务端为准）"
    log(f"登录成功 id={uid} username={username} group={group} via={result['via']}")
    log(f"checked_in={checked_in} -> {verdict}")
    log(f"当前额度 {quota_str(quota)} | 已用 {quota_str(used)}")

    rec = {
        "date_cst": today, "ts": now_cst().isoformat(timespec="seconds"),
        "success": True, "checked_in": checked_in, "verdict": verdict,
        "via": result["via"], "user_id": uid, "username": username,
        "group": group, "quota": quota, "used_quota": used,
    }
    append_history(rec)
    write_step_summary([
        f"### AgentRouter 签到 `{today}`", "",
        f"- 结论：**{verdict}**（`checked_in={checked_in}`，通道 `{result['via']}`）",
        f"- 账号：`{username}`（id `{uid}`，分组 `{group}`）",
        f"- 额度：{quota_str(quota)}",
        f"- 已用：{quota_str(used)}",
        f"- 时间：{rec['ts']}",
    ])
    tg_notify(f"✅ AgentRouter {today} {verdict}\n账号 {username}\n额度 {quota_str(quota)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
