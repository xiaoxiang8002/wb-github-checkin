# WorkBuddy 多账号自动签到（GitHub Actions 免费版）

每天自动领取 WorkBuddy 积分 + 派遣/领取猫猫旅行，跑在 GitHub Actions 上，**公开仓库永久免费**（私有仓库每月 2000 分钟也足够）。

## 为什么免费

- GitHub Actions 公开仓库执行流水线**不限分钟**；私有仓库每月 2000 分钟免费，本任务每月约 120~240 分钟，远在额度内。
- 签到逻辑是纯 Python 标准库实现，无需任何付费依赖。
- 相比腾讯云 SCF：SCF 满 3 个月后每月扣 ¥12.8 基础套餐费，且当时为看日志开通的 CLS 日志服务一直按量计费——这正是之前欠费的来源。本方案完全不碰腾讯云。

## 安全说明

- 4 个账号的 Bearer token 存放在**仓库 Secret `WB_ACCOUNTS`**（GitHub 加密存储），不进代码、不进 git 历史。
- 公开仓库仅暴露源码与运行简报；简报只含账号名、连续签到天数、猫猫状态、凭证剩余天数，**不含任何 token 原文**。
- 建议账号名用默认「账号1/2/3/4」之类，避免暴露真实标识。

## 工作原理

```
本机(Windows)  renew_github.py  ──扫描 WorkBuddy 进程内存提取最新 token──►  推送 GitHub Secret WB_ACCOUNTS
                                                                                    │
GitHub Actions  checkin.yml  ──每3小时定时──► 读 Secret ──► 调 WorkBuddy 接口完成签到+猫猫旅行
```

- **云端（GitHub Actions）**：只负责定时执行 `checkin.py`，从 Secret 读凭证，调官方 HTTPS 接口。云上读不到你本机进程，所以续期必须在本地做。
- **本机（续期）**：`renew_github.py` 从 WorkBuddy 进程内存提取最新 token，推到 Secret。注册了每周计划任务自动跑，无需手动操作。
- **保活**：GitHub 会在仓库 60 天无提交后自动停用定时任务。`renew_github.py` 每次续期做一次空提交+推送，保持仓库活跃。

## 一次性部署步骤

在已装好 Python 3.10+ 和 Git 的 Windows 上：

1. 先登录 GitHub CLI（只需一次，会打开浏览器/设备码）：
   ```
   gh auth login
   ```
2. 在本项目目录运行部署脚本（建公开仓库、推送代码、设 Secret、注册续期任务）：
   ```
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```
3. 部署脚本会调用 `renew_github.py` 提取 token——**请确保 WorkBuddy 桌面端已启动并登录**（至少登录主号；其余账号由之前的后台采集已写入 `D:\Work\wb-cloud-checkin\secrets.local.json`，会作为回退源一起推送）。

完成后 GitHub Actions 即接管自动签到，公开仓库免费、永久运行。

## 日常维护

- **token 续期**：已注册每周自动续期任务 `WB-GitHub-Token-Renew`。只要 WorkBuddy 在某周一开着，就会自动刷新 Secret。
- **手动续期**：`python renew_github.py`
- **手动触发一次签到**：GitHub 仓库 → Actions → WorkBuddy 自动签到 → Run workflow
- **查看结果**：GitHub 仓库 → Actions 运行记录，或 Step Summary 里的简报

## 文件说明

| 文件 | 作用 |
|---|---|
| `checkin.py` | 云端执行的纯签到逻辑（标准库，零依赖） |
| `.github/workflows/checkin.yml` | 每 3 小时定时触发 |
| `renew_github.py` | 本机提取 token 并推送 Secret + 注册自动续期 |
| `setup.ps1` | 一键建仓库/推送/设密钥/注册续期 |
| `accounts.json` | 仅本地调试用，已被 .gitignore 忽略，禁止提交 |

## 从腾讯云迁移

1. 本方案部署完成后，确认 GitHub Actions 至少成功跑通一次（Actions 页面看结果）。
2. 再到腾讯云：删除 SCF 函数 `wb-daily-claim`、删定时触发器；在日志服务 CLS 里删除对应日志主题，停止 CLS 扣费。
3. 删除本机旧的腾讯云计划任务 `WB-Account-Collector`（如仍在）：`python D:\Work\wb-cloud-checkin\collect_accounts.py --uninstall`

## 故障排查

- **Actions 运行报 NO_ACCOUNTS**：Secret `WB_ACCOUNTS` 未设置或为空。在本机跑 `python renew_github.py` 重新推送。
- **某账号失败 / 凭证剩余 <7 天告警**：该账号 token 临近过期。确保 WorkBuddy 登录该账号后跑一次 `python renew_github.py`。
- **定时任务不再运行**：可能仓库被 GitHub 停用（60 天无活动）。到 Actions 页启用，续期任务的空提交会自动恢复活跃。
