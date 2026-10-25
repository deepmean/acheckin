#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentRouter 每日自动签到（登录即签到）
站点: https://agentrouter.org

站点机制（2026-10 实测）
  1. 没有独立签到路由：POST /api/user/checkin -> 404（网关 Invalid URL）
  2. 每日首次登录即完成签到，登录响应 data.checked_in 就是签到结果
     前端原文: data.checked_in ? "签到成功，新增额度已到账" : "登录成功！"
  3. session Cookie + 请求头 New-Api-User: <用户id> 才能读 /api/user/self

网络现实
  agentrouter.org 前置阿里云 WAF。住宅/办公 IP 直接放行；
  GitHub Actions（Azure 数据中心 IP）会被投送「滑动验证」挑战页。
  因此本脚本走双通道：
    通道 A  requests 纯 HTTP（快，住宅 IP 直接成功）
    通道 B  Playwright 真实 Chromium（跑 WAF 的 JS、必要时拖滑块），在浏览器内部
            fetch 调用登录接口，Cookie 天然一致

环境变量
  AGENTROUTER_USERNAME   必填
  AGENTROUTER_PASSWORD   必填
  AGENTROUTER_BASE       可选，默认 https://agentrouter.org
  CHECKIN_MODE           可选 auto(默认) | requests | browser
  BACKUP_RUN             可选 '1'：当天已成功则跳过
  TG_BOT_TOKEN/TG_CHAT_ID 可选，Telegram 推送
  DEBUG_DUMP             可选，打印异常响应原文
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

BASE = (os.getenv("AGENTROUTER_BASE") or "https://agentrouter.org").rstrip("/")
USERNAME = (os.getenv("AGENTROUTER_USERNAME") or "").strip()
PASSWORD = os.getenv("AGENTROUTER_PASSWORD") or ""
MODE = (os.getenv("CHECKIN_MODE") or "auto").strip().lower()
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


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def log(msg: str) -> None:
    print(f"[{now_cst():%Y-%m-%d %H:%M:%S} CST] {msg}", flush=True)


def is_waf_page(text: str) -> bool:
    head = text[:4000]
    return any(m in head for m in WAF_MARKERS)


# --------------------------------------------------------------------------- #
# 通道 A: 纯 HTTP
# --------------------------------------------------------------------------- #
def new_session() -> requests.Session:
    s = requests.Session()
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
        "Connection": "keep-alive",
    })
    return s


def http_login(attempts: int = 3) -> dict:
    """返回 login data dict；失败抛 RuntimeError。WAF 拦截抛 WafBlocked。"""
    url = f"{BASE}/api/user/login?turnstile="
    payload = {"username": USERNAME, "password": PASSWORD}
    last_err = "未知错误"
    for i in range(1, attempts + 1):
        s = new_session()
        try:
            r = s.post(url, json=payload, timeout=30)
            text = r.text or ""
            if is_waf_page(text):
                raise WafBlocked(f"阿里云 WAF 挑战页（{len(text)} bytes）")
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {text[:160]}"
                log(f"[http] 第 {i}/{attempts} 次：{last_err}")
            else:
                try:
                    body = r.json()
                except ValueError:
                    last_err = f"HTTP {r.status_code} 非 JSON: {text[:160]}"
                    log(f"[http] 第 {i}/{attempts} 次：{last_err}")
                    if DEBUG:
                        log(f"---- raw body ({len(text)} bytes) ----\n{text[:4000]}\n---- end ----")
                else:
                    if not body.get("success"):
                        raise RuntimeError(f"登录被拒: {body.get('message') or body}")
                    data = body.get("data") or {}
                    if not isinstance(data, dict) or not data.get("id"):
                        raise RuntimeError(f"登录响应异常: {str(body)[:300]}")
                    return {"data": data, "session": s, "mode": "http"}
        except WafBlocked:
            raise
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"[http] 第 {i}/{attempts} 次网络异常：{last_err}")
        if i < attempts:
            time.sleep(5 * i)
    raise RuntimeError(f"HTTP 通道重试 {attempts} 次仍失败: {last_err}")


class WafBlocked(RuntimeError):
    pass


def http_self(session: requests.Session, uid: str) -> dict:
    try:
        r = session.get(f"{BASE}/api/user/self",
                        headers={"New-Api-User": str(uid)}, timeout=25)
        return (r.json() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"[http] 读取 /api/user/self 失败（不影响签到结论）：{type(exc).__name__}: {exc}")
        return {}


# --------------------------------------------------------------------------- #
# 通道 B: Playwright 真实浏览器（过阿里云 WAF 挑战）
# --------------------------------------------------------------------------- #
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
window.chrome = window.chrome || {runtime: {}, app: {isInstalled: false}};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(p)
  );
}
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, p);
};
"""

SLIDER_SELECTORS = [
    "#aliyunCaptcha-sliding-slider",
    "#nc_1_n1z",
    ".nc_iconfont.btn_slide",
    ".aliyun-captcha .btn_slide",
    "[id*='sliding-slider']",
]


def _human_drag(page, box: dict) -> None:
    """把滑块往右拖到底，带加速度和轻微抖动。"""
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    total = 300 + random.randint(0, 40)
    step = 0
    moved = 0
    while moved < total:
        step += 1
        # 先快后慢再微调，模拟人手
        if step < 8:
            d = random.uniform(12, 22)
        elif step < 20:
            d = random.uniform(6, 12)
        else:
            d = random.uniform(1, 4)
        d = min(d, total - moved)
        moved += d
        page.mouse.move(x + moved, y + random.uniform(-1.5, 1.5))
        time.sleep(random.uniform(0.008, 0.03))
    time.sleep(0.15)
    page.mouse.move(x + total + 2, y)
    time.sleep(0.1)
    page.mouse.up()


def _solve_slider(page) -> bool:
    for sel in SLIDER_SELECTORS:
        try:
            el = page.query_selector(sel)
        except Exception:  # noqa: BLE001
            continue
        if not el:
            continue
        try:
            box = el.bounding_box()
        except Exception:  # noqa: BLE001
            box = None
        if not box:
            continue
        log(f"[browser] 发现滑块 {sel}，尝试拖拽")
        try:
            _human_drag(page, box)
        except Exception as exc:  # noqa: BLE001
            log(f"[browser] 拖拽异常：{type(exc).__name__}: {exc}")
            continue
        page.wait_for_timeout(3500)
        if "aliyunCaptcha" not in page.content()[:4000]:
            log("[browser] 滑块后挑战页消失")
            return True
    return False


def browser_login(wait_seconds: int = 90) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--lang=zh-CN",
                "--window-size=1366,768",
            ],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        try:
            log(f"[browser] 打开 {BASE}/login")
            page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60_000)

            deadline = time.time() + wait_seconds
            solved = False
            tries = 0
            while time.time() < deadline:
                body = page.content()
                if not is_waf_page(body):
                    solved = True
                    break
                tries += 1
                log(f"[browser] 命中 WAF 挑战（第 {tries} 次检测），尝试处理")
                if _solve_slider(page):
                    solved = True
                    break
                # 有些挑战同样会自行放行，等一会儿再判断
                page.wait_for_timeout(6000)
                if tries >= 3:
                    page.reload(wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(6000)

            if not solved and is_waf_page(page.content()):
                raise RuntimeError("WAF 挑战未通过（数据中心 IP 被持续拦截）")

            log(f"[browser] 页面就绪: {page.title()[:60]}")
            res = page.evaluate(
                """async ([user, pwd]) => {
                    const r = await fetch('/api/user/login?turnstile=', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        credentials: 'include',
                        body: JSON.stringify({username: user, password: pwd}),
                    });
                    const t = await r.text();
                    let j = null;
                    try { j = JSON.parse(t); } catch (e) {}
                    return {status: r.status, json: j, raw: t.slice(0, 400)};
                }""",
                [USERNAME, PASSWORD],
            )
            body = res.get("json") or {}
            if not body.get("success"):
                raise RuntimeError(f"浏览器内登录被拒: {body.get('message') or res.get('raw')}")
            data = body.get("data") or {}
            if not data.get("id"):
                raise RuntimeError(f"浏览器内登录响应异常: {str(body)[:300]}")

            self_info = {}
            try:
                sres = page.evaluate(
                    """async (uid) => {
                        const r = await fetch('/api/user/self', {
                            headers: {'New-Api-User': String(uid)},
                            credentials: 'include',
                        });
                        const t = await r.text();
                        try { return JSON.parse(t); } catch (e) { return {raw: t.slice(0, 300)}; }
                    }""",
                    data["id"],
                )
                self_info = (sres or {}).get("data") or {}
            except Exception as exc:  # noqa: BLE001
                log(f"[browser] 读取 self 失败（不影响结论）：{type(exc).__name__}: {exc}")

            return {"data": data, "self": self_info, "mode": "browser"}
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 记录与通知
# --------------------------------------------------------------------------- #
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
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text,
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"Telegram 推送失败: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def main() -> int:
    if not USERNAME or not PASSWORD:
        log("缺少 AGENTROUTER_USERNAME / AGENTROUTER_PASSWORD")
        return 2

    today = now_cst().strftime("%Y-%m-%d")
    if BACKUP_RUN and already_ok_today(today):
        log(f"{today} 已签到成功，备份批次跳过")
        write_step_summary([f"### AgentRouter 签到 `{today}`", "", "备份批次：今日已成功，跳过。"])
        return 0

    log(f"开始签到 {BASE} | 账号 {USERNAME} | 日期 {today} | 模式 {MODE}")

    result: dict | None = None
    errors: list[str] = []

    if MODE in ("auto", "requests"):
        try:
            result = http_login()
            log("[http] 通道成功")
        except WafBlocked as exc:
            errors.append(f"http: {exc}")
            log(f"[http] 被 WAF 拦截：{exc}")
        except RuntimeError as exc:
            errors.append(f"http: {exc}")
            log(f"[http] 失败：{exc}")

    if result is None and MODE in ("auto", "browser"):
        try:
            result = browser_login()
            log("[browser] 通道成功")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"browser: {type(exc).__name__}: {exc}")
            log(f"[browser] 失败：{type(exc).__name__}: {exc}")

    if result is None:
        err = " | ".join(errors) or "未知错误"
        rec = {"date_cst": today, "ts": now_cst().isoformat(timespec="seconds"),
               "success": False, "checked_in": None, "error": err[:400]}
        append_history(rec)
        write_step_summary([f"### AgentRouter 签到 `{today}` 失败", "", f"```\n{err[:1500]}\n```"])
        tg_notify(f"❌ AgentRouter 签到失败 {today}\n{err[:300]}")
        return 1

    data = result["data"]
    uid = str(data.get("id"))
    username = data.get("username") or ""
    checked_in = bool(data.get("checked_in"))
    info = result.get("self") or http_self(result.get("session"), uid)
    quota = info.get("quota", data.get("quota"))
    used = info.get("used_quota", data.get("used_quota"))
    group = info.get("group") or data.get("group")

    verdict = "签到成功，新增额度已到账" if checked_in else "登录成功（今日签到状态以服务端为准）"
    log(f"登录成功: id={uid} username={username} group={group} via={result.get('mode')}")
    log(f"checked_in={checked_in} -> {verdict}")
    log(f"当前额度: {quota_str(quota)} | 已用: {quota_str(used)}")

    rec = {
        "date_cst": today,
        "ts": now_cst().isoformat(timespec="seconds"),
        "success": True,
        "checked_in": checked_in,
        "verdict": verdict,
        "mode": result.get("mode"),
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
        f"- 结论：**{verdict}**（`checked_in={checked_in}`，通道 `{result.get('mode')}`）",
        f"- 账号：`{username}`（id `{uid}`，分组 `{group}`）",
        f"- 额度：{quota_str(quota)}",
        f"- 已用：{quota_str(used)}",
        f"- 时间：{rec['ts']}",
    ])
    tg_notify(f"✅ AgentRouter {today} {verdict}\n账号 {username}\n额度 {quota_str(quota)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
