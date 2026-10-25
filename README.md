# aCheckin — AgentRouter 每日自动签到

每天 **北京时间 03:03** 自动登录 <https://agentrouter.org> 完成签到，签到记录写入 `state/history.jsonl` 并由 Actions 自动提交回仓库。

## 站点签到机制（实测 2026-10）

| 端点 | 结果 | 说明 |
| --- | --- | --- |
| `POST /api/user/checkin` | `404 Invalid URL` | 该站**没有**独立签到路由，网关直接拦截 |
| `POST /api/user/login?turnstile=` | `200` | **登录即签到**，响应 `data.checked_in` 即签到结果 |
| `GET /api/user/self` | `200` | 需 `session` Cookie + 请求头 `New-Api-User: <用户id>` |

前端原文逻辑：`data.checked_in ? "签到成功，新增额度已到账" : "登录成功！"` —— 所以本项目的签到动作就是**每日一次登录**。

## 目录

```
checkin.py                    签到主程序（登录即签到 + 额度快照 + 历史落盘）
requirements.txt              requests
.github/workflows/checkin.yml 定时任务（北京 03:03 主批次 / 03:33 备份批次）
state/history.jsonl           每日签到结果（自动提交）
```

## 配置

仓库 Secrets（Settings → Secrets and variables → Actions）：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `AGENTROUTER_USERNAME` | ✅ | 登录邮箱 |
| `AGENTROUTER_PASSWORD` | ✅ | 登录密码 |
| `TG_BOT_TOKEN` | ⬜ | Telegram 推送机器人 token |
| `TG_CHAT_ID` | ⬜ | Telegram 推送 chat id |

仓库 Variables：`AGENTROUTER_BASE`（默认 `https://agentrouter.org`）。

CLI 一次性写入：

```bash
gh secret set AGENTROUTER_USERNAME -R deepmean/acheckin -b 'deepmean@gmail.com'
gh secret set AGENTROUTER_PASSWORD -R deepmean/acheckin -b '<密码>'
```

## 手动触发 / 本地运行

```bash
gh workflow run checkin.yml -R deepmean/acheckin

export AGENTROUTER_USERNAME='deepmean@gmail.com'
export AGENTROUTER_PASSWORD='<密码>'
python -m pip install -r requirements.txt && python checkin.py
```

## 说明

- cron 用 UTC：`3 19 * * *` = 次日 03:03 北京。GitHub 定时任务在高峰期可能延迟几分钟到半小时，故设 `03:33` 备份批次；备份批次读取 `state/history.jsonl`，当天已成功则直接跳过。
- 登录失败（密码错误）不重试直接报错；网络异常/429/5xx 重试 3 次（间隔 5s、10s）。
- 仓库 60 天无活动会自动停用定时任务，本项目每天提交 `history.jsonl`，天然保持活跃。
