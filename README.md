# aCheckin — AgentRouter 每日自动签到

每天在**两个随机时间点**自动登录 <https://agentrouter.org> 完成签到（登录即签到），
两点严格相隔 **12 小时**、分钟**避开整点**（如今天 04:33 / 16:33，明天 00:19 / 12:19）。
结果写入 `state/history.jsonl` 并由 Actions 自动提交回仓库。

```
gate.py                        槽位闸门：算当天随机时间，作业内精确等待（纯标准库）
checkin.py                     签到主程序（登录=签到 + 额度快照 + 历史落盘）
requirements.txt               requests[socks]
.github/workflows/checkin.yml  两次日级 cron（北京 00:05 / 12:05）→ 等到抽中的分钟再签到
state/history.jsonl            每日签到结果（含 slot 字段 A/B）
```

## 一、站点签到机制（2026-10 实测）

| 端点 | 结果 | 说明 |
| --- | --- | --- |
| `POST /api/user/checkin` | `404 Invalid URL` | 该站**没有**独立签到路由 |
| `POST /api/user/login?turnstile=` | `200` | **登录即签到**，`data.checked_in` 即签到结果 |
| `GET /api/user/self` | `200` | 需 `session` Cookie + 请求头 `New-Api-User: <用户id>` |

前端原文逻辑：`data.checked_in ? "签到成功，新增额度已到账" : "登录成功！"`。
所以「签到」这件事 = **每天成功登录一次**，本项目就干这件事。

## 二、为什么不能直接在 GitHub Runner 上跑（关键坑）

`agentrouter.org` 前置**阿里云 WAF**，按出口 IP 信誉分流。实测数据：

| 测试 | 结果 |
| --- | --- |
| 家庭/办公 IP 直连 | ✅ 直接放行，登录成功 `checked_in=True` |
| GitHub Actions 出口（Azure，如 128.24.161.21 / 20.169.73.144） | ❌ 全部返回 16011 字节「滑动验证」挑战页 |
| 换任意 UA / 全量浏览器请求头 / 任意方法 / 任意路径 | ❌ 仍被挑战 |
| 备用域名 `https://ps.air-outer.com`（同一套站点） | ❌ 同样被挑战（WAF 认 IP，不认域名） |
| 带真实浏览器的 `acw_tc` + `session` Cookie | ❌ 仍被挑战（Cookie 与 IP/指纹绑定） |
| 真实 Chrome + Xvfb 拖滑块（Windows UA + 指纹补丁 + 拟人轨迹） | ❌ 返回「验证失败，请刷新」 |
| **Cloudflare WARP 出网（socks5，出口 104.28.201.80）** | ✅ **直接放行，登录成功 `checked_in=True`** |

结论：**WAF 拦截发生在认证之前，换 GitHub OAuth 登录、换 access_token 都没用**；
唯一稳定解法是把出口 IP 换掉 —— 本项目用 Cloudflare WARP。

## 三、随机时间机制

`gate.py` 以**北京日期**为随机种子，当天固定生成一个偏移量：

```
off ∈ 0..300 分钟随机
分钟 m = (5 + off) % 60     → 落在 3..57，永不整点（排除 0/1/2/58/59）
槽位 A = 00:05 + off        槽位 B = A 班实际执行时刻 + 12 小时
```

B 班跟随 A 的**实际**执行时刻，所以即使 A 班因调度延迟晚了几小时，间隔仍是 12 小时。
同一天内多次运行结果一致（不会漂移），跨天重新抽签。实测抽签：

```
2026-10-25  off=268min  A=04:33  B=16:33
2026-10-26  off= 14min  A=00:19  B=12:19
2026-10-27  off=  7min  A=00:12  B=12:12
2026-10-28  off=110min  A=01:55  B=13:55
2026-10-30  off=164min  A=02:49  B=14:49
2026-11-01  off=  5min  A=00:10  B=12:10
```

## 三之二、执行方式：24 条 cron + 闸门窗口 + 去重

```yaml
schedule:
  - cron: '5 * * * *'   # 每小时 :05，全天 24 条
```

每条 cron 只是「醒来问一句」，`gate.py` 决定这次要不要签：

| 情况 | 决策 |
| --- | --- |
| 两个槽位当天都已完成 | `skip` — 10 秒结束，不装依赖、不连 WARP、不提交 |
| 目标在未来 40 分钟内 | 等到目标前 120 秒放行，由签到步骤精确对齐目标秒 |
| 目标已过、槽位未完成 | 立即补签（迟到多少都签） |
| 目标还在 40 分钟之外 | `skip`，把机会让给后面的 cron |

**等待上限只有 40 分钟**，这是硬约束：GitHub 托管 runner 会杀掉长时间空转的 job
（10-29 实测等 112 分钟 → `lost communication`，job 2h15m 后被杀）。
`timeout-minutes: 60` 与之匹配，`concurrency` 也不会堆积。

## 三之三、为什么不信单条 cron（实测台账）

| 观察 | 结果 |
| --- | --- |
| 每小时 cron `'7 * * * *'` | ❌ 3 个整点全空 |
| `'*/11 * * * *'` 探针 | ❌ 25 分钟一次没跑 |
| 日级 A 班（10-26） | ⚠️ 触发，迟到 **2h51m** |
| 日级 B 班（10-26） | ❌ 当天没来；次日 18:00 才补触发（+5h55m），闸门按去重逻辑跳过 |
| 日级 A 班（10-27） | ⚠️ 触发，迟到 **4h54m**（04:59:55 执行，签到成功、额度 +12,500,000） |
| 日级 B 班（10-27） | ❌ 到 17:30 仍未触发（+5h25m），靠 `slot=auto` 补签 |
| 12 条/天（10-28） | ✅ A 命中（+41s）、B 命中（+39s），间隔 12h00m39s |
| 长等待（10-29 13:37，等了 112 分钟） | ❌ runner `lost communication`，job 2h15m 后被杀，B 漏签 |
| GitHub Status | ✅ Actions operational |

**结论：**
1. GitHub 自带 cron 只能保证「每天大概醒几次」，且迟到 2.5~6 小时 —— 用「多条机会 + 去重」而不是「一条精确 cron」。
2. **job 里不能睡几小时**：等 112 分钟会被判定失联直接杀掉。现在等待上限压到 **40 分钟**，
   cron 提到 **每小时一条（24 条/天）**，保证总有一条落在目标前 40 分钟窗口内。

## 三之四、要严格 12 小时：接一个外部定时器（可选）

`workflow_dispatch` 支持 `slot=auto`：闸门会挑当天还没成功的槽位、算出目标时刻、
必要时在作业内等待（≤150 分钟）再签到。所以只要**外部每 12 小时调一次 dispatch**，
两次签到就严格相隔 12 小时，且分钟仍由当天种子决定（非整点）。

外部定时器（cron-job.org / 阿里云函数 / 任意 uptime 机器人）配置两个任务：

```
POST https://api.github.com/repos/deepmean/acheckin/actions/workflows/checkin.yml/dispatches
Headers:
  Authorization: Bearer <PAT>
  Accept: application/vnd.github+json
Body:
  {"ref": "main", "inputs": {"slot": "auto"}}
```

- 任务 1：每天 00:05（北京时间）
- 任务 2：每天 12:05（北京时间）

PAT 用 fine-grained token，最少权限：`Actions: Read and write`。

命令行等价：

```bash
gh workflow run checkin.yml -R deepmean/acheckin --ref main -f slot=auto
```

没接外部定时器时，12 条 GitHub cron 仍会兜底；闸门按 `history.jsonl`
去重，同一槽位当天成功过就不会重复签。

## 四、出口 IP 方案

```
checkin job → 等到当天抽中的分钟 → 安装并连接 WARP(proxy 模式, 127.0.0.1:40000)
  → CHECKIN_PROXY=socks5h://127.0.0.1:40000 → checkin.py
     通道1 代理(WARP) 登录=签到
     通道2 直连兜底（自托管 runner / 家宽 IP / WARP 挂了时）
  → 写 state/history.jsonl → 自动提交回仓库
```

WARP 免费、无需账号；日志里会明确写出 `via=socks5h://...` 还是 `via=direct`。

## 五、配置

仓库 Secrets（Settings → Secrets and variables → Actions）：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `AGENTROUTER_USERNAME` | ✅ | 登录邮箱 |
| `AGENTROUTER_PASSWORD` | ✅ | 登录密码 |
| `TG_BOT_TOKEN` | ⬜ | Telegram 推送 bot token |
| `TG_CHAT_ID` | ⬜ | Telegram 推送 chat id |

仓库 Variables（可选）：

| 名称 | 说明 |
| --- | --- |
| `AGENTROUTER_BASE` | 站点地址，默认 `https://agentrouter.org` |
| `RUNNER_LABEL` | 自托管 runner 标签，如 `[self-hosted, windows]`；设了就不装 WARP，走家宽直连 |
| `DEBUG_DUMP` | 设 `1` 时打印异常响应原文 |

```bash
gh secret set AGENTROUTER_USERNAME -R deepmean/acheckin -b 'you@example.com'
gh secret set AGENTROUTER_PASSWORD -R deepmean/acheckin -b '<密码>'
```

## 六、运行与验证

```bash
gh workflow run checkin.yml -R deepmean/acheckin   # 手动触发（slot=manual，立即执行）
gh run list -R deepmean/acheckin -L 5              # 看结果
python gate.py                                     # 本地看今天抽到哪两个时间
```
本地直连（家宽 IP 可直连）：

```bash
export AGENTROUTER_USERNAME='you@example.com'
export AGENTROUTER_PASSWORD='<密码>'
python -m pip install -r requirements.txt && python checkin.py
```

本地模拟 CI 的 WARP 代理：

```bash
export CHECKIN_PROXY=socks5h://127.0.0.1:40000
python checkin.py
```

## 七、注意事项

- cron 是 UTC：`7 * * * *` 表示每小时第 7 分钟，与北京时间的小时天然对齐（北京 = UTC+8，整小时偏移），所以闸门里的"小时"判断不会错位。
- GitHub 定时任务高峰期可能延迟数分钟甚至一小时；命中窗口的「目标后 2 小时」保证延迟后依然会补签。
- 仓库 60 天无活动会自动停用定时任务；本项目每天提交 `history.jsonl`，天然保持活跃。
- 登录失败（如密码错误）不重试直接报错；网络异常/429/5xx 每个通道重试 2 次。
