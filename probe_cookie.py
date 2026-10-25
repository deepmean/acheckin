#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侦察 3：用真实浏览器 Cookie 从数据中心 IP 请求，看阿里云 WAF 是否放行。"""
import json
import os

import requests

BASE = "https://agentrouter.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
USER = os.getenv("AGENTROUTER_USERNAME", "")
PWD = os.getenv("AGENTROUTER_PASSWORD", "")
SESS = os.getenv("AGENTROUTER_SESSION_COOKIE", "").strip()
ACW = os.getenv("AGENTROUTER_ACW_TC", "").strip()
MARKERS = ("aliyun_waf", "访问验证", "滑动验证", "aliyunCaptcha")

BASE_HDRS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="24", "Google Chrome";v="141"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def run(label, method, path, cookies=None, json_body=None, extra=None):
    hdrs = dict(BASE_HDRS)
    if extra:
        hdrs.update(extra)
    if method == "GET":
        r = requests.get(BASE + path, headers=hdrs, cookies=cookies or {}, timeout=30)
    else:
        hdrs["Content-Type"] = "application/json"
        hdrs["Origin"] = BASE
        hdrs["Referer"] = BASE + "/login"
        r = requests.post(BASE + path, headers=hdrs, cookies=cookies or {},
                          json=json_body, timeout=30)
    t = r.text or ""
    waf = any(m in t[:5000] for m in MARKERS)
    body = ""
    if not waf:
        try:
            j = r.json()
            body = json.dumps({k: j.get(k) for k in ("success", "message")}, ensure_ascii=False)
            if isinstance(j.get("data"), dict):
                d = j["data"]
                body += " data=" + json.dumps(
                    {k: d.get(k) for k in ("id", "username", "checked_in", "quota", "used_quota")},
                    ensure_ascii=False)
        except ValueError:
            body = t[:150].replace("\n", " ")
    print(f"### {label}\n    HTTP {r.status_code} len={len(t)} waf={waf}  {body[:300]}")
    return r


def main():
    print(f"egress ip = {requests.get('https://api.ipify.org', timeout=20).text}")
    print(f"session len = {len(SESS)}  acw_tc = {ACW[:16]}...")

    run("1 GET /api/status | 无 cookie", "GET", "/api/status")
    if ACW:
        run("2 GET /api/status | acw_tc only", "GET", "/api/status", {"acw_tc": ACW})
    if SESS:
        run("3 GET /api/status | session only", "GET", "/api/status", {"session": SESS})
    if SESS and ACW:
        both = {"acw_tc": ACW, "session": SESS}
        run("4 GET /api/status | acw_tc + session", "GET", "/api/status", both)
        run("5 GET /api/user/self | acw_tc + session + New-Api-User", "GET", "/api/user/self",
            both, extra={"New-Api-User": "622724"})
        run("6 POST /api/user/login | acw_tc + session", "POST", "/api/user/login?turnstile=",
            both, json_body={"username": USER, "password": PWD})
    print("\n(说明: waf=True 表示仍是挑战页；waf=False 才算穿过 WAF)")


if __name__ == "__main__":
    main()
