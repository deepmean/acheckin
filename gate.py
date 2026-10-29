#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
槽位闸门（纯标准库，不装任何依赖）

需求：每天随机两个签到时间，两者严格相隔 12 小时，分钟避开整点。

实测到的四件事实（2026-10-25 ~ 10-29）：
  1. 高频 cron 会被丢：'7 * * * *' 连续 3 个整点没触发，'*/11' 探针 25 分钟一次没跑。
  2. 日级 cron 会触发但**迟到**：+2h51m / +4h54m。
  3. 日级第二条 cron（B 班）连续两天**整天没触发**（次日才补跑）。
  4. 等待 112 分钟的 job 被 GitHub 判「lost communication」直接杀掉（10-29 13:37 那次），
     说明长等待不可靠 —— 现在把等待压到 40 分钟以内。
  => 不能指望「某一条 cron 一定在某个时刻醒」，也不能在 job 里睡几小时；
     只能靠「多条机会 + 短等待 + 去重」。

设计：
  · workflow 每小时唤醒一次（cron '5 * * * *'），全天 24 条，不区分 A/B。
  · 闸门按当天计划挑槽位：
        槽位 A = 00:05 + off       （off ∈ 0..300 分钟随机）
        槽位 B = A 班实际执行时刻 + 12 小时（A 没成功则退回 12:05 + off）
    判据：
        目标已过、槽位未完成      -> 立即补签
        目标在未来 WAIT_AHEAD 内  -> 等到目标前 LEAD_SECONDS 放行
        目标还很远                -> 本次跳过，把机会让给后面的 cron
        两个槽位都完成            -> 跳过
  · 等待 ≤40 分钟（远低于会被 runner 回收的时长），LEAD_SECONDS=120 用来装 WARP，
    真正到点由 checkin.py 精确对齐目标秒（实测误差 <5s）。
  · 分钟 m = (5 + off) % 60 落在 3..57，永远避开整点。
  · 同槽位当天成功后写入 history.jsonl，后续 cron 读到即跳过 —— 去重靠它，不靠 cron 次数。

环境变量
  EVENT_NAME   github.event_name
  SLOT         auto（默认）/ A / B / manual
  SLOT_MODE    wait（默认）/ now（立即）

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
BASE_MINUTE = 5       # 与 cron 的分钟对齐
OFF_MAX = 300         # 随机偏移上限（分钟）
WAIT_AHEAD = 40       # 目标在未来这么多分钟内才等；长等待会被 runner 回收（10-29 实测）
LEAD_SECONDS = 120    # 只等到目标前 2 分钟就放行，把装 WARP 的时间塞进这段空档，
                      # 由 checkin.py 的 wait_for_target 精确等到目标秒（误差 <5s）
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


def slot_target(rows: list[dict], now: dt.datetime, slot: str) -> dt.datetime:
    """槽位目标时刻。B 跟随 A 的实际执行时刻 + 12 小时。"""
    p = plan(now)
    target = now.replace(hour=p[slot][0], minute=p[slot][1], second=0, microsecond=0)
    if slot == "B":
        a_ts = slot_actual_ts(rows, now.strftime("%Y-%m-%d"), "A")
        if a_ts:
            target = a_ts + dt.timedelta(hours=12)
    return target


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
    slot_env = (os.getenv("SLOT") or "auto").strip().upper()

    outputs = {"slot": "manual", "target": "", "skip": "0"}
    print(f"[gate] 现在 {now:%Y-%m-%d %H:%M:%S} CST")
    print(f"[gate] 今日计划 {plan_text(now)}")

    if slot_env == "MANUAL":
        outputs["slot"] = "manual"
        outputs["target"] = now.isoformat(timespec="seconds")
        print("[gate] 手动立即模式 -> 不等待")
    else:
        rows = history_rows()
        done = done_slots(rows, today)
        print(f"[gate] 今日已完成槽位: {sorted(done) or '无'}")

        slot, target, decision = None, None, "skip"
        order = [slot_env] if slot_env in ("A", "B") else ["A", "B"]
        for s in order:
            if s in done:
                print(f"[gate] 槽位 {s} 已完成，跳过")
                continue
            t = slot_target(rows, now, s)
            delta = (now - t).total_seconds()
            if s == "B":
                a_ts = slot_actual_ts(rows, today, "A")
                if a_ts:
                    print(f"[gate] A 班实际执行 {a_ts:%H:%M:%S} -> B 目标 {t:%H:%M:%S}（严格 12 小时）")
            if delta >= 0:
                slot, target, decision = s, t, "late"
                print(f"[gate] 槽位 {s} 目标 {t:%H:%M:%S} 已过 {delta / 60:.0f} 分钟 -> 立即补签")
                break
            if -delta <= WAIT_AHEAD * 60:
                slot, target, decision = s, t, "wait"
                print(f"[gate] 槽位 {s} 目标 {t:%H:%M:%S}，{-delta / 60:.0f} 分钟后 -> 等待")
                break
            print(f"[gate] 槽位 {s} 目标 {t:%H:%M:%S} 还有 {-delta / 60:.0f} 分钟，"
                  f"超出 {WAIT_AHEAD} 分钟窗口 -> 让给后面的 cron")

        if slot is None:
            outputs["slot"] = "none"
            outputs["skip"] = "1"
            print("[gate] 本次不签到")
        else:
            if mode == "now":
                print(f"[gate] 立即模式：不等窗口直接执行槽位 {slot}")
            elif decision == "wait":
                lead = target - dt.timedelta(seconds=LEAD_SECONDS)
                if lead > dt.datetime.now(CST):
                    sleep_until(lead)
                    print(f"[gate] 已到目标前 {LEAD_SECONDS}s，放行给签到步骤精确对齐目标秒")
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
