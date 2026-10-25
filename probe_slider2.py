#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侦察 5：滑块验证破解加固版 —— Windows UA + 指纹补丁 + 网络诊断 + 多次重试。"""
import json
import os
import time

BASE = "https://agentrouter.org"
WIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
USER = os.getenv("AGENTROUTER_USERNAME", "")
PWD = os.getenv("AGENTROUTER_PASSWORD", "")
MARKERS = ("aliyun_waf", "访问验证", "滑动验证", "aliyunCaptcha")

STEALTH = r"""
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
Object.defineProperty(navigator,'maxTouchPoints',{get:()=>0});
window.chrome = {runtime:{}, app:{isInstalled:false}, csi:()=>{}, loadTimes:()=>{}};
const gp = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p){
  if(p===37445) return 'Intel Inc.';
  if(p===37446) return 'Intel Iris OpenGL Engine';
  return gp.call(this,p);
};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p)=> p && p.name==='notifications'
  ? Promise.resolve({state: Notification.permission}) : origQuery(p);
if (navigator.userAgentData) {
  Object.defineProperty(navigator,'userAgentData',{get:()=>({
    brands:[{brand:'Chromium',version:'141'},{brand:'Google Chrome',version:'141'},
            {brand:'Not?A_Brand',version:'24'}],
    mobile:false, platform:'Windows'})});
}
"""


def has_waf(page) -> bool:
    try:
        c = page.content()
    except Exception:  # noqa: BLE001
        return False
    return any(m in c[:5000] for m in MARKERS)


def human_drag(page, start_x, start_y, distance):
    page.mouse.move(start_x, start_y)
    page.wait_for_timeout(180)
    page.mouse.down()
    page.wait_for_timeout(120)
    moved = 0.0
    events = 0
    while moved < distance - 1:
        frac = moved / max(distance, 1)
        step = (3.0 + 7.0 * (1 - frac)) * (0.85 + 0.3 * (0.5 - abs(0.5 - (frac))))
        step = max(step, 1.5)
        moved = min(moved + step, distance)
        page.mouse.move(start_x + moved, start_y + (0.8 if events % 7 else -0.9), steps=1)
        events += 1
        page.wait_for_timeout(10 + (events % 5) * 3)
    page.wait_for_timeout(220)
    page.mouse.move(start_x + distance + 1.5, start_y)
    page.wait_for_timeout(90)
    page.mouse.up()
    return events


def attempt(page, idx):
    handle = page.query_selector("#aliyunCaptcha-sliding-slider")
    track = page.query_selector("#aliyunCaptcha-sliding-wrapper")
    if not handle or not track:
        print(f"  [{idx}] 找不到滑块元素")
        return False
    hb, tb = handle.bounding_box(), track.bounding_box()
    if not hb or not tb:
        print(f"  [{idx}] 几何信息缺失")
        return False
    sx = hb["x"] + hb["width"] / 2
    sy = hb["y"] + hb["height"] / 2
    dist = tb["x"] + tb["width"] - (hb["x"] + hb["width"]) - 2
    n = human_drag(page, sx, sy, dist)
    page.wait_for_timeout(4200)
    err = ""
    for sel in ["#aliyunCaptcha-sliding-errorCode", "#aliyunCaptcha-sliding-failTip",
                "#aliyunCaptcha-sliding-text"]:
        el = page.query_selector(sel)
        if el:
            t = (el.inner_text() or "").strip()
            if t:
                err += f" {sel}={t!r}"
    gone = not has_waf(page)
    print(f"  [{idx}] drag events={n} dist={dist:.0f} waf_gone={gone}{err}")
    return gone


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False, channel="chrome", args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--disable-dev-shm-usage", "--lang=zh-CN", "--start-maximized"])
        ctx = b.new_context(user_agent=WIN_UA, locale="zh-CN", timezone_id="Asia/Shanghai",
                            viewport={"width": 1600, "height": 900},
                            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        ctx.add_init_script(STEALTH)
        page = ctx.new_page()

        seen = []

        def on_resp(r):
            u = r.url
            if any(k in u for k in ("captcha", "verify", "acw", "waf", "slide")):
                try:
                    seen.append(f"{r.status} {u[:110]} :: {(r.text() or '')[:120]}")
                except Exception:  # noqa: BLE001
                    seen.append(f"{r.status} {u[:110]}")

        page.on("response", on_resp)

        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8000)
        print(f"title={page.title()[:40]!r} ua={page.evaluate('navigator.userAgent')[:80]}")
        print(f"platform={page.evaluate('navigator.platform')} webdriver={page.evaluate('navigator.webdriver')}")

        passed = False
        for i in range(1, 5):
            if not has_waf(page):
                passed = True
                break
            if attempt(page, i):
                passed = True
                break
            page.wait_for_timeout(2500)

        print(f"challenge_passed={passed}")
        print("---- captcha network ----")
        for line in seen[-25:]:
            print("   ", line)

        if passed:
            page.wait_for_timeout(2500)
            res = page.evaluate("""async ([u,p]) => {
                const r = await fetch('/api/user/login?turnstile=', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    credentials:'include', body: JSON.stringify({username:u, password:p})});
                const t = await r.text();
                let j=null; try{j=JSON.parse(t)}catch(e){}
                return {status:r.status, ok:!!(j&&j.success), msg:(j&&j.message)||'',
                        checked_in: j&&j.data&&j.data.checked_in, text:t.slice(0,120)};
            }""", [USER, PWD])
            print("login:", json.dumps(res, ensure_ascii=False))
        page.screenshot(path="challenge.png")
        b.close()


if __name__ == "__main__":
    main()
