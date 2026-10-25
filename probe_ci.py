#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性侦察脚本：在 CI 出口 IP 上测试 WAF 对不同路径/头/认证方式的表现。"""
import json
import os
import sys

import requests

BASE = "https://agentrouter.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
USER = os.getenv("AGENTROUTER_USERNAME", "")
PWD = os.getenv("AGENTROUTER_PASSWORD", "")
MARKERS = ("aliyun_waf", "访问验证", "Access Verification", "aliyunCaptcha", "滑动验证")


def classify(r):
    t = r.text or ""
    waf = any(m in t[:4000] for m in MARKERS)
    return f"HTTP {r.status_code} len={len(t)} waf={waf} ct={r.headers.get('content-type','')[:40]}"


def show(label, r, keep=200):
    print(f"\n### {label}\n    {classify(r)}")
    print("    " + (r.text or "").replace("\n", " ")[:keep])


def main():
    ip = requests.get("https://api.ipify.org", timeout=20).text
    print(f"egress ip = {ip}")

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

    show("A GET /api/status (browser UA)", s.get(f"{BASE}/api/status", timeout=30))
    show("B GET / (browser UA)", s.get(f"{BASE}/", timeout=30))
    show("C GET /api/status (curl UA)", requests.get(
        f"{BASE}/api/status", headers={"User-Agent": "curl/8.5.0"}, timeout=30))
    show("D GET /api/status (python-requests UA)", requests.get(f"{BASE}/api/status", timeout=30))
    show("E GET /api/status (empty UA)", requests.get(
        f"{BASE}/api/status", headers={"User-Agent": " "}, timeout=30))

    hdrs = {
        "User-Agent": UA, "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": BASE, "Referer": f"{BASE}/login",
        "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
    }
    show("F POST /api/user/login (full browser headers)",
         requests.post(f"{BASE}/api/user/login?turnstile=", json={"username": USER, "password": PWD},
                       headers=hdrs, timeout=30), keep=260)
    show("G POST /api/user/login (bare)",
         requests.post(f"{BASE}/api/user/login?turnstile=", json={"username": USER, "password": PWD},
                       timeout=30), keep=260)
    show("H GET /api/user/self (no cookie)",
         requests.get(f"{BASE}/api/user/self", headers=hdrs, timeout=30), keep=160)

    # Playwright 真实浏览器
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"\n### I playwright 不可用: {exc}")
        return

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--disable-dev-shm-usage", "--lang=zh-CN"])
        ctx = b.new_context(user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
                            viewport={"width": 1366, "height": 768})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        p = ctx.new_page()
        p.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
        p.wait_for_timeout(8000)
        body = p.content()
        waf = any(m in body[:4000] for m in MARKERS)
        print(f"\n### I playwright goto /login\n    title={p.title()[:50]!r} waf={waf} len={len(body)}")
        if waf:
            for sel in ["#aliyunCaptcha-sliding-slider", "#nc_1_n1z", ".btn_slide",
                        "[id*='slider']", "[class*='slide']"]:
                el = p.query_selector(sel)
                print(f"    slider[{sel}] = {'FOUND' if el else 'none'}")
        res = p.evaluate("""async ([u,pw]) => {
            const r = await fetch('/api/user/login?turnstile=', {
                method:'POST', headers:{'Content-Type':'application/json'},
                credentials:'include', body: JSON.stringify({username:u, password:pw})});
            const t = await r.text();
            let j=null; try{j=JSON.parse(t)}catch(e){}
            return {status:r.status, ok: !!(j&&j.success), msg: (j&&j.message)||'', text:t.slice(0,160)};
        }""", [USER, PWD])
        print(f"### J in-browser fetch login\n    {json.dumps(res, ensure_ascii=False)}")
        b.close()


if __name__ == "__main__":
    sys.exit(main())
