#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
槽位闸门（纯标准库，不装任何依赖）

需求：每天随机两个签到时间，两者严格相隔 12 小时，分钟避开整点。

为什么不是「每小时 cron + 闸门」：
  实测本账号下高频 cron 会被 GitHub 丢掉（*/11 探测 25 分钟一次没跑，
  每小时 :07 连续三次全空），而同账号另一个仓库的**日级 cron** 每天都正常出跑。
  所以这里改成「两次日级 cron + 作业内精确等待」：
    cron 05:16 UTC = 北京 00:05 启动 A 班
    cron 05:04 UTC = 北京 12:05 启动 B 班
  两班各自等到当天抽中的那一分钟再签到 —— 调度晚几分钟甚至一小时都不影响结果。

随机规则（以北京日期为种子，当天固定）：
  off ∈ 0..300 分钟随机
  m   = (5 + off) % 60，落在 3..57 之间（避开整点 0/1/2 分与 58/59 分）
  槽位 A = 00:m + off 分钟     槽位 B = 12:m + off 分钟   ← 严格相隔 12 小时

环境变量
  EVENT_NAME   github.event_name（schedule / workflow_dispatch）
  SLOT         A | B（schedule 时由 workflow 传入；dispatch 走 manual）
  SLOT_MODE    wait（默认，等到目标分钟）/ now（立即执行）

$GITHUB_OUTPUT
  slot / target / skip
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))
BASE_MINUTE = 5      # 与 cron 的分钟对齐：00:05 / 12:05
OFF_MAX = 300        # 随机偏移上限（分钟）= 5 小时，作业最长占用远小于 6h 上限
BAD_MINUTES = {0, 1, 2, 58, 59}   # 避开整点及其前后

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "state" / "history.jsonl"


def offset_minutes(now: dt.datetime) -> int:
    """当天固定的随机偏移（同日多次调用一致）。"""
    rnd = random.Random(f"agentrouter:{now:%Y-%m-%d}")
    for _ in range(200):
        off = rnd.randint(0, OFF_MAX)
        if (BASE_MINUTE + off) % 60 not in BAD_MINUTES:
            return off
    return 7


def plan(now: dt.datetime) -> dict[str, tuple[int, int]]:
    off = offset_minutes(now)
    minute = (BASE_MINUTE + off) % 60
    hour_a = (0 + (BASE_MINUTE + off) // 60) % 24           # 00:05 + off
    hour_b = (12 + (BASE_MINUTE + off) // 60) % 24          # 12:05 + off
    return {"A": (hour_a, minute), "B": (hour_b, minute)}


def plan_text(now: dt.datetime) -> str:
    p = plan(now)
    return (f"A={p['A'][0]:02d}:{p['A'][1]:02d} "
            f"B={p['B'][0]:02d}:{p['B'][1]:02d} (北京时间, 间隔 12 小时)")


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


def sleep_until(target: dt.datetime) -> None:
    while True:
        left = (target - dt.datetime.now(CST)).total_seconds()
        if left <= 0:
            return
        if left > 600:
            print(f"[gate] 距目标还有 {left / 60:.0f} 分钟，继续等待", flush=True)
            time.sleep(600)
        else:
            print(f"[gate] 最后等待 {left:.0f}s", flush=True)
            time.sleep(left)
            return


def main() -> int:
    now = dt.datetime.now(CST)
    today = f"{now:%Y-%m-%d}"
    mode = (os.getenv("SLOT_MODE") or "wait").strip().lower()
    event = (os.getenv("EVENT_NAME") or "").strip()
    slot_env = (os.getenv("SLOT") or "").strip().upper()

    outputs = {"slot": "manual", "target": "", "skip": "0"}
    print(f"[gate] 现在 {now:%Y-%m-%d %H:%M:%S} CST")
    print(f"[gate] 今日计划 {plan_text(now)}")

    if event == "workflow_dispatch" or slot_env == "MANUAL":
        outputs["slot"] = "manual"
        outputs["target"] = now.isoformat(timespec="seconds")
        print("[gate] 手动触发 -> 立即执行，不等待")
    else:
        slot = "B" if slot_env == "B" else "A"
        p = plan(now)
        target = now.replace(hour=p[slot][0], minute=p[slot][1], second=0, microsecond=0)
        done = done_slots(today)

        if slot in done:
            outputs["skip"] = "1"
            print(f"[gate] 槽位 {slot} 今日已完成，跳过")
        elif mode == "now":
            print(f"[gate] 槽位 {slot} 立即模式，目标 {target:%H:%M}")
        elif target <= now:
            print(f"[gate] 槽位 {slot} 目标 {target:%H:%M} 已过（调度延迟），立即执行")
        else:
            print(f"[gate] 槽位 {slot} 目标 {target:%H:%M}，开始等待")
            sleep_until(target)
            print(f"[gate] 到达目标时间 {dt.datetime.now(CST):%H:%M:%S}")

        outputs["slot"] = slot
        outputs["target"] = target.isoformat(timespec="seconds")

    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in outputs.items():
                fh.write(f"{k}={v}\n")
    print(f"[gate] 结果 slot={outputs['slot']} skip={outputs['skip']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
