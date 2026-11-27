# aCheckin — 多站点每日自动签到

**当前：AgentRouter 继续运行；DGB 默认停用，恢复仅支持导入已有会话。**

每天在**两个随机时间点**自动签到，两点严格相隔 **12 小时**、分钟**避开整点**
（如今天 04:33 / 16:33，明天 00:19 / 12:19）。
结果写入 `state/history*.jsonl` 并由 Actions 自动提交回仓库。

已接入：

| 站点 | 脚本 | 历史文件 | 出口 |
| --- | --- | --- | --- |
| AgentRouter（agentrouter.org） | `checkin.py` | `state/history.jsonl` | Cloudflare WARP |
| DGB 公益站（freeapi.dgbmc.top） | `checkin_dgb.py` | `state/history_dgb.jsonl` | 直连 |
| RunningHub（www.runninghub.ai） | `checkin_rh.py` | `state/history_rh.jsonl` | 直连 |
| Mengzhiji（mengzhiji.net） | `checkin_mzj.py` | `state/history_mzj.jsonl` | 直连 |

```
gate.py                        槽位闸门：算当天随机时间，作业内精确等待（纯标准库）
checkin.py                     签到主程序：AgentRouter（登录=签到 + 额度快照）
checkin_dgb.py                 签到主程序：DGB 公益站（POST /api/user/checkin）
requirements.txt               requests[socks]
.github/workflows/checkin.yml  每小时 cron → 两个站点依次签到
state/history.jsonl            AgentRouter 每日结果（含 slot 字段 A/B）
state/history_dgb.jsonl        DGB 每日结果（含 slot / 奖励额度）
```

## DGB：仅复用已有会话（默认停用）

DGB 自动签到默认关闭，AgentRouter 独立运行。
旧版每次密码登录会创建新会话，且未保存 Refresh Cookie，导致历史会话累积；
仅持有账号密码无法取回这些会话的 Refresh Cookie。本版本已经移除密码登录和自动注销路径。

### 复用协议

1. 导入现有会话的 `new_api_refresh` Cookie，可同时提供原 `sid`。
2. `POST /api/user/auth/refresh` 获取短期 Bearer，验证用户名及原 SID 一致。
3. 接收轮换后的 Refresh Cookie 并立即加密保存；随后查询/执行签到。
4. 保留该 SID 供下次使用，只关闭本地 HTTP 连接，既不新建登录会话，也不注销原会话。

| 配置 | 类型 | 用途 |
| --- | --- | --- |
| `DGB_ENABLED` | Repository Variable | 只有精确值 `true` 开启，缺省关闭 |
| `DGB_ACCOUNT_NAMES` | Repository Variable | 用户名 JSON 数组，缺省 `["fjpg5som8g","deepmean"]` |
| `DGB_SESSIONS` | Secret | 现有会话种子，格式见下 |
| `DGB_SESSION_KEY` | Secret | Fernet 加密密钥；`Fernet.generate_key()` 生成，保留不变 |
| `DGB_BASE` | Repository Variable | 缺省 `https://freeapi.dgbmc.top` |

`DGB_SESSIONS` 的格式（通过 GitHub Secrets 界面配置）：

```json
{
  "account1": {"refresh_cookie": "<existing new_api_refresh value>", "sid": "<existing sid>"},
  "account2": {"refresh_cookie": "<existing new_api_refresh value>"}
}
```

请使用一份专用于自动化的已有会话，交由脚本独占刷新；浏览器与脚本同时轮换同一 Cookie
会造成竞争。导入后不要再让原浏览器刷新该会话，也不要在该浏览器点退出该会话。
如果已无任何有效 Cookie，DGB 维持停用，待站方释放会话或正常恢复账号后再导入。

### 跨运行保存与异常处理

- `state/dgb_sessions.enc` 只存 Fernet 密文，密钥只存在 GitHub Secret；明文 Cookie 和 Bearer 不进入提交、日志或 Actions 输出。
- 全部运行串行，开始时读取主分支最新状态。旧种子不会覆盖已经轮换的新 Cookie。
- 在发出刷新请求前，先将绑定 `run_id` 和 `run_attempt` 的准备检查点加密提交远端；提交成功才放行刷新。
- 刷新中断、失效、账号/SID 不一致或重定向时隔离该会话；下一次运行拒绝重放，不会回退密码登录。
- 若 runner 丢失或最后提交失败，远端保留的准备标记同样会阻止后续运行重放旧 Cookie。此时需要重新导入有效会话。
- 即使签到失败，轮换状态也会在 `always()` 步骤保存；关闭 DGB 后，其历史不再参与 AgentRouter 的槽位交集。
- 每个账号独立记录；复用开启时，只有全部指定账号完成才视为 DGB 槽位完成。

旧 `DGB_PASSWORD*` 和含密码的 `DGB_ACCOUNTS` Secrets 已从工作流环境移除。
本地运行同样默认关闭，显式配置 `DGB_ENABLED=true`、会话与密钥才会请求 DGB。

安装依赖后执行 `python -m unittest discover -v`，使用本地 HTTP 服务验证：默认停用零请求、
连续同 SID 刷新、轮换持久化、异常隔离以及缺少会话时零密码登录。所有测试使用虚拟凭据。

## RunningHub（www.runninghub.ai）每日积分

每天 30 POINT，北京时间 00:00 重置。**密码只在一次性初始化时使用，日常运行只用 refresh_token。**

| 端点 | 说明 |
| --- | --- |
| `POST /uc/abroad/pwdLogin` | 一次性初始化：`{email, password}`，password 为 32 位 MD5；返回 `access_token` + `refresh_token`（30 天） |
| `POST /uc/token/refresh` | 头 `Authorization: Bearer <refresh_token>` 换取新 access；实测**非轮换**，旧 refresh 仍可用 |
| `POST /api/welfare/summary` | 读 `data.dailyGift.granted` 判断今日是否已领 |
| `POST /api/welfare/daily-gift/claim` | 领取；首次 `data=true`，重复 `data=false` 且余额不变（服务端幂等） |
| `POST /uc/point/summary` / `gainList` | 积分余额与发放记录 |

### 配置

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `RH_ACCOUNTS` | Secret | `[{"email":"...","refresh_token":"..."}, ...]`，9 个账号 |
| `RH_ACCOUNT_EMAILS` | Variable | 仅邮箱的 JSON 数组，供 `gate.py` 做槽位完成判定（不含令牌） |
| `RH_BASE` | Variable | 可选，默认 `https://www.runninghub.ai` |

### 一次性初始化

`bootstrap_rh.py` 在**本机**运行（不进 Actions），用密码为每个账号换取 refresh_token 并写入本地文件，
再导入成 `RH_ACCOUNTS`。之后工作流不再挂载任何 RunningHub 密码。

### 调度与去重

RunningHub 与 AgentRouter 共用同一套随机槽位：每天两个随机时间、间隔 12 小时、分钟避开整点。
`gate.py` 要求**当天同一槽位的全部 9 个账号**都有成功记录，槽位才算完成；因此某个账号失败时，
下一条 cron 会继续补签。第二个槽位拿到「今日已领取」也会记一条成功记录，用于兜底确认。

- 令牌缺失、格式错误或刷新被拒时，该账号单独失败并提示重新导入，**不会回退到密码登录**。
- 所有请求禁用重定向，避免 3xx 被自动转发到登录端点。

### 每日登录次数与会话

该站**签到即登录**，没有独立的签到路由（`POST /api/user/checkin` 返回 404），
也没有可用的注销接口（`/api/user/auth/logout` 与 `/api/user/logout` 实测均 404）。
因此**无法做到"只刷新不登录"**——想拿当天奖励就必须登录一次。

可以做到的是把登录压到每天最多一次：

- 当天已有成功记录时，后续槽位直接记一条成功并跳过登录（`checked_in_today`）。
- 只有当天首次执行、或此前失败时才真正登录，从而把会话增速从 2/天 降到 1/天。
- 站点按 IP 限流（429）并对数据中心出口返回 WAF 挑战页，因此不做额外探测请求。

## Mengzhiji（mengzhiji.net）每日签到

每日登录 Cookie 保存在 `MZJ_SESSIONS`，日常先复用 HttpOnly 会话：
`GET /api/rewards/daily-checkin` 检查 `claimedToday`，未领取时才 `POST` 同一路径。
重复领取由服务端标记，调度端仍按账号+槽位去重。站点没有 refresh token；当前 Cookie 有约 14 天有效期。

| 配置 | 类型 | 说明 |
| --- | --- | --- |
| `MZJ_ACCOUNTS` | Secret | base64 JSON 数组：`[{"email":"...","password":"..."}, ...]`；密码仅在会话 401 后兜底使用 |
| `MZJ_SESSIONS` | Secret | base64 JSON 映射 `{email: novel_session_cookie}`，初始 Cookie |
| `MZJ_SESSION_KEY` | Secret | Fernet 密钥，兜底登录签发的新 Cookie 加密后保存 |
| `MZJ_ACCOUNT_EMAILS` | Variable | 两个邮箱的逗号分隔列表，用于 gate 槽位完成判定 |
| `MZJ_BASE` | Variable | 默认 `https://api.mengzhiji.net` |

密码兜底只在 `/api/auth/me` 明确返回 401 时发生；429、403、网络错误不会触发登录。
兜底后的会话写入 `state/mzj_sessions.enc`（密文），原始 Cookie 和密码不进入日志或历史文件。
每天与 AgentRouter 共用两个随机槽位、间隔 12 小时、分钟避开整点；只有该槽位两个账号都成功才算完成。

## 一、AgentRouter 站点签到机制（2026-10 实测）

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

每个 job 依次跑两个站点（同一套随机槽位）：

| 站点 | 脚本 | 出口 |
| --- | --- | --- |
| AgentRouter（agentrouter.org） | `checkin.py` | 走 Cloudflare WARP（阿里云 WAF 只认 IP 信誉） |
| DGB 公益站（freeapi.dgbmc.top） | `checkin_dgb.py` | 直连（无 WAF 拦截） |

每条 cron 只是「醒来问一句」，`gate.py` 决定这次要不要签：

| 情况 | 决策 |
| --- | --- |
| 两个槽位当天都已完成 | `skip` — 10 秒结束，不装依赖、不连 WARP、不提交 |
| 目标在未来 85 分钟内 | 等到目标前 120 秒放行，由签到步骤精确对齐目标秒 |
| 目标已过、槽位未完成 | 立即补签（迟到多少都签） |
| 目标还在 85 分钟之外 | `skip`，把机会让给后面的 cron |

**等待上限 85 分钟**，这是权衡出来的：GitHub 托管 runner 会杀掉长时间空转的 job
（10-29 实测等 112 分钟 → `lost communication`），85 分钟留了余量，同时等待期间每 5 分钟
打一条心跳日志保持活跃。`timeout-minutes: 100` 与之匹配。

## 三之三、为什么不信单条 cron（实测台账）

| 观察 | 结果 |
| --- | --- |
| 每小时 cron `'7 * * * *'` | ❌ 3 个整点全空 |
| `'*/11 * * * *'` 探针 | ❌ 25 分钟一次没跑 |
| 日级 A 班（10-26） | ⚠️ 触发，迟到 **2h51m** |
| 日级 B 班（10-26） | ❌ 当天没来；次日 18:00 才补触发（+5h55m），闸门按去重逻辑跳过 |
| 日级 A 班（10-27） | ⚠️ 触发，迟到 **4h54m**（04:59:55 执行，签到成功、额度 +12,500,000） |
| 日级 B 班（10-27） | ❌ 到 17:30 仍未触发（+5h25m），靠 `slot=auto` 补签 |
| 12 条/天（10-28） | ✅ A 命中（+41s）、B 命中（+39s），间隔 **12h00m39s** |
| 长等待（10-29 13:37，等了 112 分钟） | ❌ runner `lost communication`，job 2h15m 后被杀，B 漏签 |
| 24 条/天（10-30） | ⚠️ 全天只到 4 条 cron；A 补签于 06:41，B 目标 18:41 被整段错过，19:17 人工补签 |
| 24 条/天（10-31） | ⚠️ 全天只到 4 条 cron；A 补签于 05:26，16:20 那条距 B 目标 65 分钟（当时窗口 55）→ 错过，20:05 人工补签 |
| GitHub Status | ✅ Actions operational |

### 六天实测间隔（每天两次都成功，但间隔从未稳定在 12 小时）

| 日期 | A | B | 实测间隔 | 说明 |
| --- | --- | --- | --- | --- |
| 10-26 | 02:57:48 | 15:43:03 | 12h45m15s | B 班 cron 当日全丢，dispatch 补签 |
| 10-27 | 04:59:55 | 17:32:34 | 12h32m39s | 同上 |
| 10-28 | 01:55:41 | 13:56:20 | **12h00m39s** | 唯一一次两次全自动且准点 |
| 10-29 | 03:29:14 | 18:07:39 | 14h38m25s | B 班 job 被 runner 回收 |
| 10-30 | 06:41:21 | 19:17:44 | 12h36m23s | B 目标前后 cron 全丢 |
| 10-31 | 05:26:01 | 20:05:45 | 14h39m44s | 16:20 那条差 10 分钟没进窗口，人工补签 |
| 11-01 ~ 11-08 | — | — | 13h28m ~ 17h16m | 上线 85 分钟窗口后间隔仍未收敛，cron 到达率约 3~4/24 |

**这说明「靠 GitHub cron 兜住 12 小时」已到极限**：签到从不漏，但间隔在 9h46m ~ 17h17m 之间漂。
要稳定 12 小时，只能换触发源（见下「外部定时器」）。

**结论：**
1. GitHub 自带 cron 只能保证「每天大概醒几次」，到达率实测约 **4/24**，且迟到 2.5~6 小时
   —— 它足以保证「不漏签」（补签逻辑兜底），但**无法保证「严格 12 小时」**。
2. **job 里不能睡几小时**：等 112 分钟会被判定失联直接杀掉。等待上限 55 分钟，
   cron 每小时一条，`timeout-minutes: 75`。
3. 要真正稳定在 12 小时，只能让触发源可靠 —— 见「三之四、外部定时器」。

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
| `AGENTROUTER_USERNAME` | ✅ | AgentRouter 登录邮箱 |
| `AGENTROUTER_PASSWORD` | ✅ | AgentRouter 登录密码 |
| `DGB_SESSIONS` | 可选 | DGB 已有会话 Cookie，仅显式开启复用时需要 |
| `DGB_SESSION_KEY` | 可选 | Fernet 加密密钥，仅显式开启复用时需要 |
| `TG_BOT_TOKEN` | ⬜ | Telegram 推送 bot token |
| `TG_CHAT_ID` | ⬜ | Telegram 推送 chat id |

仓库 Variables（可选）：

| 名称 | 说明 |
| --- | --- |
| `AGENTROUTER_BASE` | 站点地址，默认 `https://agentrouter.org` |
| `DGB_BASE` | 站点地址，默认 `https://freeapi.dgbmc.top` |
| `DGB_ENABLED` | 默认 `false`；仅 `true` 开启旧会话复用 |
| `DGB_ACCOUNT_NAMES` | 用户名 JSON 数组，默认 `["fjpg5som8g","deepmean"]` |
| `RUNNER_LABEL` | 自托管 runner 标签，如 `[self-hosted, windows]`；设了就不装 WARP，走家宽直连 |
| `DEBUG_DUMP` | 设 `1` 时打印异常响应原文 |

```bash
gh secret set AGENTROUTER_USERNAME -R deepmean/acheckin -b 'you@example.com'
gh secret set AGENTROUTER_PASSWORD -R deepmean/acheckin -b '<密码>'
gh variable set DGB_ENABLED -R deepmean/acheckin -b 'false'
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
