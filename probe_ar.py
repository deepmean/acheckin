"""Recon: does AgentRouter support a refresh-token flow separate from login?

Login is the check-in on this site, so the question is whether an existing
session cookie / refresh cookie survives without a new login, and whether any
separate check-in route exists behind the session.
Prints structure and status codes only; cookie values are never printed.
"""
import json
import os
import requests

BASE = (os.getenv("AGENTROUTER_BASE") or "https://agentrouter.org").rstrip("/")
EMAIL = os.environ["AR_EMAIL"]
PASSWORD = os.environ["AR_PASSWORD"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

s = requests.Session()
s.trust_env = False
s.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/login",
})

r = s.post(BASE + "/api/user/login?turnstile=",
           json={"username": EMAIL, "password": PASSWORD}, timeout=45)
print("login HTTP", r.status_code, "len", len(r.text))
try:
    body = r.json()
except ValueError:
    print("non-json (WAF?) prefix:", r.text[:80])
    raise SystemExit(1)

print("success:", body.get("success"), "message:", str(body.get("message"))[:80])
data = body.get("data") or {}
if isinstance(data, dict):
    for key, value in data.items():
        if isinstance(value, str) and len(value) > 24:
            print(f"  data.{key}: <str len={len(value)}>")
        else:
            print(f"  data.{key}: {value}")

print("cookies:", ", ".join(sorted(s.cookies.keys())))
uid = str(data.get("id") or data.get("user_id") or "")

headers = {"New-Api-User": uid} if uid else {}
print("\n-- /api/user/self --")
r2 = s.get(BASE + "/api/user/self", headers=headers, timeout=30)
print("HTTP", r2.status_code, str(r2.text)[:200])

print("\n-- candidate check-in routes (session-based) --")
for path in ("/api/user/checkin", "/api/user/check_in", "/api/user/daily-checkin",
             "/api/user/signin", "/api/user/checkin/status", "/api/user/checkin?month="):
    try:
        rr = s.post(BASE + path, headers=headers, json={}, timeout=20)
        print(f"  POST {path} -> {rr.status_code} {str(rr.text)[:110]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  POST {path} -> ERR {type(exc).__name__}")

print("\n-- refresh endpoints --")
for path in ("/api/user/auth/refresh", "/api/user/token/refresh", "/api/user/refresh"):
    try:
        rr = s.post(BASE + path, headers=headers, json={}, timeout=20)
        print(f"  POST {path} -> {rr.status_code} {str(rr.text)[:140]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  POST {path} -> ERR {type(exc).__name__}")