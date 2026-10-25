#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侦察 2：真实 Chrome + Xvfb 下尝试通过阿里云滑动验证。"""
import json
import os
import random
import time

BASE = "https://agentrouter.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
USER = os.getenv("AGENTROUTER_USERNAME", "")
PWD = os.getenv("AGENTROUTER_PASSWORD", "")
MARKERS = ("aliyun_waf", "访问验证", "滑动验证", "aliyunCaptcha")


def waf(content: str) -> bool:
    return any(m in content[:5000] for m in MARKERS)


def drag(page, box, distance):
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(120, 260))
    page.mouse.down()
    page.wait_for_timeout(random.randint(90, 180))
    moved = 0.0
    t0 = time.time()
    while moved < distance - 1:
        # 抖动+变速，模拟人手
        frac = moved / max(distance, 1)
        speed = 1.0 - 0.75 * frac
        step = random.uniform(3.5, 9.0) * max(speed, 0.18)
        moved = min(moved + step, distance)
        page.mouse.move(x + moved, y + random.uniform(-1.2, 1.2), steps=2)
        page.wait_for_timeout(random.randint(8, 26))
    # 收尾微调
    for _ in range(random.randint(2, 4)):
        page.mouse.move(x + distance + random.uniform(-1.5, 0.5), y + random.uniform(-0.8, 0.8))
        page.wait_for_timeout(random.randint(30, 70))
    page.wait_for_timeout(random.randint(180, 420))
    page.mouse.up()
    print(f"    drag 用时 {time.time()-t0:.2f}s 距离 {distance:.0f}px")


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--lang=zh-CN", "--start-maximized"],
        )
        ctx = browser.new_context(
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport=None,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
            Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
            window.chrome = window.chrome || {runtime:{}};
        """)
        page = ctx.new_page()
        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(9000)
        body = page.content()
        print(f"title={page.title()[:60]!r} waf={waf(body)} len={len(body)}")
        print(f"ua={page.evaluate('navigator.userAgent')[:90]}")
        print(f"webdriver={page.evaluate('navigator.webdriver')}")

        if waf(body):
            html = page.evaluate("""() => {
                const el = document.querySelector('.aliyun-captcha') ||
                           document.querySelector('#aliyunCaptcha-sliding-wrapper') ||
                           document.querySelector('[id*=aliyunCaptcha]');
                return el ? el.outerHTML.slice(0, 2500) : 'NOT FOUND';
            }""")
            print("---- captcha DOM ----")
            print(html)
            print("---- end DOM ----")

            handle = page.query_selector("#aliyunCaptcha-sliding-slider")
            track = None
            for sel in ["#aliyunCaptcha-sliding-track", ".aliyun-captcha-track",
                        "#aliyunCaptcha-sliding-wrapper", "[id*=Track]"]:
                track = page.query_selector(sel)
                if track:
                    print(f"track selector = {sel}")
                    break
            hb = handle.bounding_box() if handle else None
            tb = track.bounding_box() if track else None
            print(f"handle box={hb}")
            print(f"track  box={tb}")
            if hb and tb:
                distance = tb["x"] + tb["width"] - (hb["x"] + hb["width"]) - 2
                print(f"planned distance = {distance:.1f}")
                drag(page, hb, distance)
                page.wait_for_timeout(5000)
                body2 = page.content()
                print(f"after drag: title={page.title()[:60]!r} waf={waf(body2)} len={len(body2)}")
                # 失败时常见提示文本
                for sel in [".aliyun-captcha-tips", "#aliyunCaptcha-msg", "[class*=tips]"]:
                    el = page.query_selector(sel)
                    if el:
                        print(f"tip[{sel}] = {el.inner_text()[:120]!r}")
            else:
                print("!! 找不到滑块/轨道几何信息")

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
        page.screenshot(path="challenge.png", full_page=False)
        browser.close()


if __name__ == "__main__":
    main()
