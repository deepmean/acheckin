#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
槽位闸门（纯标准库，不装任何依赖）

需求：每天随机两个签到时间，两者严格相隔 12 小时，分钟避开整点。

实测到的两件事实（2026-10-25/26）：
  1. 高频 cron 会被丢：'7 * * * *' 连续 3 个整点没触发，'*/11' 探针 25 分钟一次没跑。
  2. 日级 cron 会触发，但**迟到**：A 班 cron 定在 16:05 UTC，实际 18:56 UTC 才跑（+2h51m）。
     同账号另一仓库的日级 cron 也每天稳定出跑，所以日级是可靠档位，只是时刻会偏移。

因此设计成：
  · 两次日级 cron 只当「开工信号」：北京 00:05（A 班）/ 12:05（B 班）。
  · A 班：目标是当天抽中的时刻；若调度迟到（目标已过）就立即执行，
    并把「实际执行时刻」写进 history.jsonl 的 ts。
  · B 班：读 A 班的实际 ts，目标 = A 实际时刻 + 12 小时，
    这样即使 A 班迟到了 3 小时，两班间隔依旧严格 12 小时。
    A 班若当天没成功（或还没跑），B 班回退到抽中的时刻。
  · 等待上限 MAX_WAIT 分钟，保证作业不超过 GitHub 单作业 6 小时上限。

随机规则（以北京日期为种子，当天固定）：
  off ∈ 0..300 分钟随机
  m   = (5 + off) % 60，落在 3..57（避开整点 0/1/2 分与 58/59 分）
  抽中槽位 A = 00:05 + off      槽位 B = 12:05 + off

环境变量
  EVENT_NAME   github.event_name（schedule / workflow_dispatch）
  SLOT         A | B（schedule 时由 workflow 传入）
  SLOT_MODE    wait（默认）/ now（立即执行）

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
BASE_MINUTE = 5       # 与 cron 的分钟对齐：00:05 / 12:05
OFF_MAX = 300         # 随机偏移上限（分钟）
MAX_WAIT = 330        # 单次等待上限（分钟），远小于 GitHub 单作业 6 小时限制
BAD_MINUTES = {0, 1, 2, 58, 59}

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "state" / "history.jsonl"


def offset_minutes(now: dt.datetime) -> int:
    """当天固定的随机偏移（同日多次调用结果一致）。"""
    rnd = random.Random(f"agentrouter:{now:%Y-%m-%d}")
    for _ in range(200):
        off = rnd.randint(0, OFF_MAX)
        if (BASE_MINUTE + off) % 60 not in BAD_MINUTES:
            return off
    return 7


def plan(now: dt.datetime) -> dict[str, tuple[int, int]]:
    off = offset_minutes(now)
    minute = (BASE_MINUTE + off) % 60
    hour_a = (0 + (BASE_MINUTE + off) // 60) % 24
    hour_b = (12 + (BASE_MINUTE + off) // 60) % 24
    return {"A": (hour_a, minute), "B": (hour_b, minute)}


def plan_text(now: dt.datetime) -> str:
    p = plan(now)
    return (f"A={p['A'][0]:02d}:{p['A'][1]:02d} "
            f"B={p['B'][0]:02d}:{p['B'][1]:02d} (北京时间, 间隔 12 小时)")


def history_rows() -> list[dict]:
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


def done_slots(rows: list[dict], today: str) -> set[str]:
    return {str(r["slot"]) for r in rows
            if r.get("date_cst") == today and r.get("success") and r.get("slot")}


def slot_actual_ts(rows: list[dict], today: str, slot: str) -> dt.datetime | None:
    """某槽位当天成功签到的实际时刻（取最后一条）。"""
    found = None
    for r in rows:
        if r.get("date_cst") != today or r.get("slot") != slot or not r.get("success"):
            continue
        if not r.get("ts"):
            continue
        try:
            found = dt.datetime.fromisoformat(r["ts"])
        except ValueError:
            continue
    return found


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
        rows = history_rows()
        done = done_slots(rows, today)
        p = plan(now)
        target = now.replace(hour=p[slot][0], minute=p[slot][1], second=0, microsecond=0)

        if slot in done:
            done_ts = slot_actual_ts(rows, today, slot)
            outputs["slot"] = slot
            outputs["skip"] = "1"
            outputs["target"] = (done_ts or target).isoformat(timespec="seconds")
            print(f"[gate] 槽位 {slot} 今日已完成（{done_ts:%H:%M:%S}），跳过")
        else:
            if slot == "B":
                a_ts = slot_actual_ts(rows, today, "A")
                if a_ts:
                    chained = a_ts + dt.timedelta(hours=12)
                    print(f"[gate] A 班实际执行 {a_ts:%H:%M:%S} -> B 目标锁死为 {chained:%H:%M:%S}（严格 12 小时）")
                    target = chained
                else:
                    print("[gate] A 班今天没有成功记录，B 使用抽中时刻")

            ceiling = now + dt.timedelta(minutes=MAX_WAIT)
            if target > ceiling:
                print(f"[gate] 目标 {target:%H:%M:%S} 超出等待上限，改为 {ceiling:%H:%M:%S}")
                target = ceiling

            if mode == "now":
                print(f"[gate] 槽位 {slot} 立即模式")
            elif target <= now:
                delay_min = (now - target).total_seconds() / 60
                print(f"[gate] 槽位 {slot} 目标 {target:%H:%M:%S} 已过 {delay_min:.0f} 分钟"
                      f"（调度延迟），立即执行")
            else:
                print(f"[gate] 槽位 {slot} 目标 {target:%H:%M:%S}，开始等待")
                sleep_until(target)
                print(f"[gate] 到达目标时间 {dt.datetime.now(CST):%H:%M:%S}")

            outputs["slot"] = slot
            outputs["target"] = target.isoformat(timespec="seconds")

    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in outputs.items():
                fh.write(f"{k}={v}\n")
    print(f"[gate] 结果 slot={outputs['slot']} target={outputs['target']} skip={outputs['skip']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
