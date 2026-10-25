#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侦察 4：备用域名 https://ps.air-outer.com 从数据中心 IP 是否可用（无 WAF 挑战）。"""
import json
import os

import requests

ALT = "https://ps.air-outer.com"
MAIN = "https://agentrouter.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
USER = os.getenv("AGENTROUTER_USERNAME", "")
PWD = os.getenv("AGENTROUTER_PASSWORD", "")
MARKERS = ("aliyun_waf", "访问验证", "滑动验证", "aliyunCaptcha")

HDRS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def waf(t):
    return any(m in (t or "")[:5000] for m in MARKERS)


def main():
    print(f"egress ip = {requests.get('https://api.ipify.org', timeout=20).text}")
    s = requests.Session()
    s.headers.update(HDRS)

    r = s.get(f"{ALT}/api/status", timeout=30)
    print(f"### ALT GET /api/status  HTTP {r.status_code} len={len(r.text)} waf={waf(r.text)}")
    try:
        d = r.json()["data"]
        print(f"    system_name={d.get('system_name')} version={d.get('version')} "
              f"turnstile={d.get('turnstile_check')} github_oauth={d.get('github_oauth')}")
    except Exception as exc:  # noqa: BLE001
        print(f"    parse fail: {exc} :: {r.text[:200]}")

    r = s.get(f"{ALT}/", timeout=30)
    print(f"### ALT GET /            HTTP {r.status_code} len={len(r.text)} waf={waf(r.text)}")

    r = s.post(f"{ALT}/api/user/login?turnstile=",
               json={"username": USER, "password": PWD},
               headers={**HDRS, "Origin": ALT, "Referer": f"{ALT}/login",
                        "Content-Type": "application/json"}, timeout=30)
    print(f"### ALT POST /api/user/login  HTTP {r.status_code} len={len(r.text)} waf={waf(r.text)}")
    ok = False
    try:
        j = r.json()
        d = j.get("data") or {}
        ok = bool(j.get("success"))
        print(f"    success={j.get('success')} message={j.get('message')!r} "
              f"id={d.get('id')} username={d.get('username')!r} "
              f"checked_in={d.get('checked_in')} quota={d.get('quota')}")
    except Exception as exc:  # noqa: BLE001
        print(f"    parse fail: {exc} :: {r.text[:300]}")

    if ok:
        cookies = {c.name: c.value for c in s.cookies}
        print(f"    cookies={list(cookies)}  session_len={len(cookies.get('session',''))}")
        uid = str((r.json().get("data") or {}).get("id"))
        r2 = s.get(f"{ALT}/api/user/self", headers={**HDRS, "New-Api-User": uid}, timeout=30)
        info = {}
        try:
            info = (r2.json() or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            pass
        print(f"### ALT GET /api/user/self  HTTP {r2.status_code} waf={waf(r2.text)} "
              f"quota={info.get('quota')} used={info.get('used_quota')} group={info.get('group')}")

    # 对照: 主域名
    r3 = requests.get(f"{MAIN}/api/status", headers=HDRS, timeout=30)
    print(f"### MAIN GET /api/status HTTP {r3.status_code} len={len(r3.text)} waf={waf(r3.text)}")


if __name__ == "__main__":
    main()
