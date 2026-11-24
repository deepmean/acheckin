"""Recon using the known-good login flow from checkin.py.

Login on AgentRouter IS the check-in, so the question is whether an existing
session (cookie) can carry the daily action without a new password login.
Prints structure and status codes only; never prints cookie values.
"""
import os

import requests

import checkin

PROXY = os.getenv("CHECKIN_PROXY") or ""
proxies = {"http": PROXY, "https": PROXY} if PROXY else None

result = checkin.login_via("probe", proxies)
data = result["data"]
session: requests.Session = result["session"]

print("login ok; data keys:", ",".join(sorted(data.keys())))
for key in ("checked_in", "id", "username", "quota"):
    if key in data:
        print(f"  {key}: {data[key]}")
print("cookies:", ",".join(sorted(session.cookies.keys())))

uid = str(data.get("id") or "")
headers = {"New-Api-User": uid}

print("\n-- /api/user/self --")
r = session.get(checkin.BASE + "/api/user/self", headers=headers, timeout=30)
print("HTTP", r.status_code, str(r.text)[:180])

print("\n-- candidate check-in routes with existing session --")
for path in ("/api/user/checkin", "/api/user/check_in", "/api/user/daily-checkin",
             "/api/user/signin", "/api/user/task/checkin"):
    try:
        rr = session.post(checkin.BASE + path, headers=headers, json={}, timeout=20)
        print(f"  POST {path} -> {rr.status_code} {str(rr.text)[:100]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  POST {path} -> ERR {type(exc).__name__}")

print("\n-- refresh endpoints --")
for path in ("/api/user/auth/refresh", "/api/user/token/refresh", "/api/user/refresh"):
    try:
        rr = session.post(checkin.BASE + path, headers=headers, json={}, timeout=20)
        print(f"  POST {path} -> {rr.status_code} {str(rr.text)[:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  POST {path} -> ERR {type(exc).__name__}")

print("\n-- can the session cookie alone re-login (no password)? --")
try:
    rr = session.get(checkin.BASE + "/api/user/self", headers=headers, timeout=20)
    print("  self with session only ->", rr.status_code, str(rr.text)[:120])
except Exception as exc:  # noqa: BLE001
    print("  ERR", type(exc).__name__)