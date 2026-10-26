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
槽位 A = 00:05 + off        槽位 B = 12:05 + off      ← 严格相隔 12 小时（720 分钟）
```

同一天内多次运行结果一致（不会漂移），跨天重新抽签。实测抽签：

```
2026-10-25  off=268min  A=04:33  B=16:33
2026-10-26  off= 14min  A=00:19  B=12:19
2026-10-27  off=  7min  A=00:12  B=12:12
2026-10-28  off=110min  A=01:55  B=13:55
2026-10-30  off=164min  A=02:49  B=14:49
2026-11-01  off=  5min  A=00:10  B=12:10
```

**为什么不用「每小时 cron + 闸门」**（实测，2026-10-25/26）：

| 观察 | 结果 |
| --- | --- |
| 本仓库 `cron '7 * * * *'`（每小时） | ❌ 连续 2 小时、3 个整点全部没触发 |
| 本仓库 `cron '*/11 * * * *'` 探针 | ❌ 推上去 25 分钟，一次都没跑 |
| 同账号另一仓库 `zhiju2apiwrt` 的日级 cron | ✅ 每天稳定出跑 |
| 本仓库日级 cron（改版后） | ✅ 触发，但**迟到 2 小时 51 分**（16:05 UTC 的 cron 18:56 UTC 才跑） |
| GitHub Status | ✅ Actions operational |

所以换成**稀疏日级 cron + 作业内等待**，并且让 B 班**跟随 A 班的实际执行时刻**：

```
A 班：目标 = 抽中时刻；调度迟到（目标已过）就立即执行，
      实际执行时刻写进 history.jsonl 的 ts
B 班：目标 = A 班实际 ts + 12 小时   ← 即使 A 迟到 3 小时，间隔依旧严格 12 小时
      A 班当天没成功 → B 回退到抽中时刻
      等待上限 330 分钟（GitHub 单作业上限 6 小时，留足余量）
```

实测（2026-10-26）：

```
[gate] 今日计划 A=00:19 B=12:19
[gate] 槽位 A 目标 00:19:00 已过 158 分钟（调度延迟），立即执行
[gate] 登录成功 ... checked_in=True   ← 02:57:48 执行，额度 +12,500,000（约 +$25）
B 班预期：目标锁死为 14:57:48（严格 12 小时）
```

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
