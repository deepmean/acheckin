#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调度闸门（纯标准库，不装任何依赖）

需求：每天随机两个签到时间，两者严格相隔 12 小时，分钟避开整点。

做法：
  1. 以北京日期为随机种子，当天生成一个「基准小时 hA ∈ 0..11」和
     一个「分钟 m = 7..11」——同一天内多次运行结果完全一致，
     所以两个时间点必然是 hA:m 与 (hA+12):m，间隔严格 12 小时。
  2. workflow 每小时 :07 触发一次（cron '7 * * * *'），闸门判断当前是否
     落在命中窗口：
        目标前 15 分钟 ~ 目标后 2 小时
     命中才放行真正的签到 job，否则直接退出（job 只活几秒）。
  3. 分钟固定落在 7~11，永远不会是整点（不含 :00）。
  4. 目标后 2 小时的宽限窗口 = 自动补签：GitHub 定时任务偶发延迟、
     或第一次登录失败时，后续整点的 job 还会再试。

输出到 $GITHUB_OUTPUT：
  hit     = A | B | manual | none
  target  = 命中的目标时间（北京时间 ISO）
  plan    = 当天两个时间点的可读描述
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))
BASE_MINUTE = 7      # 基准分钟，避开整点
MINUTE_JITTER = 5    # 实际分钟 = 7..11
GRACE_BEFORE = 900   # 目标前 15 分钟内的 job 也放行（先等到达目标分钟）
GRACE_AFTER = 7200   # 目标后 2 小时内仍放行（补签）

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "state" / "history.jsonl"


def plan(now: dt.datetime) -> dict[str, tuple[int, int]]:
    """当天固定的两个时间点：(小时, 分钟)。同日多次调用结果一致。"""
    rnd = random.Random(f"agentrouter:{now:%Y-%m-%d}")
    hour_a = rnd.randint(0, 11)
    minute = BASE_MINUTE + rnd.randint(0, MINUTE_JITTER - 1)
    return {"A": (hour_a, minute), "B": ((hour_a + 12) % 24, minute)}


def plan_text(now: dt.datetime) -> str:
    p = plan(now)
    a = f"{p['A'][0]:02d}:{p['A'][1]:02d}"
    b = f"{p['B'][0]:02d}:{p['B'][1]:02d}"
    return f"A={a} B={b} (北京时间, 间隔 12 小时)"


def done_slots(today: str) -> set[str]:
    if not HISTORY.exists():
        return set()
    done: set[str] = set()
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("date_cst") == today and rec.get("success") and rec.get("slot"):
            done.add(str(rec["slot"]))
    return done


def main() -> int:
    now = dt.datetime.now(CST)
    today = f"{now:%Y-%m-%d}"
    text = plan_text(now)
    event = os.getenv("EVENT_NAME") or ""
    outputs = {"plan": text, "hit": "none", "target": ""}

    print(f"[gate] 现在 {now:%Y-%m-%d %H:%M:%S} CST")
    print(f"[gate] 今日计划 {text}")

    if event == "workflow_dispatch":
        outputs["hit"] = "manual"
        outputs["target"] = now.isoformat(timespec="seconds")
        print("[gate] 手动触发 -> 立即执行")
    else:
        done = done_slots(today)
        print(f"[gate] 今日已完成槽位: {sorted(done) or '无'}")
        today_plan = plan(now)
        for slot in ("A", "B"):
            if slot in done:
                print(f"[gate] 槽位 {slot} 今日已完成，跳过")
                continue
            hour, minute = today_plan[slot]
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - target).total_seconds()
            if -GRACE_BEFORE <= delta <= GRACE_AFTER:
                outputs["hit"] = slot
                outputs["target"] = target.isoformat(timespec="seconds")
                wait = max(0, int(-delta))
                print(f"[gate] 命中槽位 {slot}，目标 {target:%H:%M}，需等待 {wait}s")
                break
            print(f"[gate] 槽位 {slot} 目标 {target:%H:%M}，当前偏差 {int(delta)}s，跳过")

    if outputs["hit"] == "none":
        print("[gate] 未命中窗口，本次不签到")
    else:
        print(f"[gate] 放行: hit={outputs['hit']}")

    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in outputs.items():
                fh.write(f"{k}={v}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
