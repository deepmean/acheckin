# aCheckin — AgentRouter 每日自动签到

每天**北京时间 03:03** 自动在 <https://agentrouter.org> 完成签到（登录即签到），
结果写入 `state/history.jsonl` 并由 Actions 自动提交回仓库；另有 03:33 备份批次。

```
checkin.py                     签到主程序（登录=签到 + 额度快照 + 历史落盘）
requirements.txt               requests / requests[socks]
.github/workflows/checkin.yml  定时任务（北京 03:03 主批次、03:33 备份批次、手动触发）
state/history.jsonl            每日签到结果
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
| **Cloudflare WARP 出网（socks5，出口 104.28.227.105）** | ✅ **直接放行，登录成功 `checked_in=True`** |

结论：**WAF 拦截发生在认证之前，所以换 GitHub OAuth 登录、换 access_token 都没用**；
唯一稳定解法是把出口 IP 换掉 —— 本项目用 Cloudflare WARP。

## 三、当前实现

```
schedule(03:03 CST) → ubuntu-latest → 安装并连接 WARP(proxy 模式, 127.0.0.1:40000)
  → CHECKIN_PROXY=socks5h://127.0.0.1:40000 → checkin.py
     通道1 代理(WARP) 登录=签到
     通道2 直连兜底（自托管 runner / 家宽 IP / WARP 挂了时）
  → 写 state/history.jsonl → 自动提交回仓库
```

WARP 免费、无需账号；若某天 WARP 连不上，脚本自动回落到直连通道，
日志里会明确写出 `via=direct` 还是 `via=socks5h://...`。

## 四、配置

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

CLI 写入：

```bash
gh secret set AGENTROUTER_USERNAME -R deepmean/acheckin -b 'you@example.com'
gh secret set AGENTROUTER_PASSWORD -R deepmean/acheckin -b '<密码>'
```

## 五、运行与验证

```bash
gh workflow run checkin.yml -R deepmean/acheckin      # 手动触发
gh run list -R deepmean/acheckin -L 3                 # 看结果
```

本地（家宽 IP 可直连）：

```bash
export AGENTROUTER_USERNAME='you@example.com'
export AGENTROUTER_PASSWORD='<密码>'
python -m pip install -r requirements.txt && python checkin.py
```

本地走 WARP 代理（模拟 CI）：

```bash
export CHECKIN_PROXY=socks5h://127.0.0.1:40000
python checkin.py
```

## 六、注意事项

- cron 用 UTC：`3 19 * * *` = 次日 03:03 北京。GitHub 定时任务高峰期可能延迟数分钟到半小时，故设 03:33 备份批次；备份批次读 `state/history.jsonl`，当天已成功就跳过。
- 仓库 60 天无活动会自动停用定时任务；本项目每天提交 `history.jsonl`，天然保持活跃。
- 登录失败（如密码错误）不重试直接报错；网络异常/429/5xx 每个通道重试 2 次。
