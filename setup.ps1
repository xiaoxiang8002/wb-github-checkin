# 一键部署 WorkBuddy 签到到 GitHub Actions（公开仓库，免费）。
# 前置：已执行 `gh auth login`；WorkBuddy 桌面端已启动并登录（至少主号）。
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

Write-Host "[1/5] 检查 GitHub CLI 登录状态 ..."
gh auth status
if ($LASTEXITCODE -ne 0) {
    Write-Error "未登录 GitHub CLI。请先在本机执行：gh auth login"
    exit 1
}

Write-Host "[2/5] 初始化本地仓库并提交 ..."
if (-not (Test-Path .git)) { git init -q }
git add -A
git commit -q -m "init WorkBuddy GitHub checkin" 2>&1 | Out-Null
git branch -M main 2>&1 | Out-Null

Write-Host "[3/5] 创建公开仓库并推送 ..."
$login = (gh api user -q .login).Trim()
$repo = "wb-github-checkin"
gh repo create $repo --public --source=. --remote=origin --push `
    --description "WorkBuddy 多账号自动签到 + 猫猫旅行（GitHub Actions 免费）" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  仓库可能已存在，尝试直接推送 ..."
    git remote add origin "https://github.com/$login/$repo.git" 2>$null
    git push -u origin main
}

Write-Host "[4/5] 提取 WorkBuddy token 并推送到仓库 Secret ..."
python renew_github.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "推送 Secret 失败：请确认 WorkBuddy 已启动登录，或检查 --source 回退路径"
    exit 1
}

Write-Host "[5/5] 注册每周自动续期任务 ..."
python renew_github.py --install

Write-Host ""
Write-Host "完成。GitHub Actions 已接管自动签到（公开仓库，免费）。"
Write-Host "查看运行结果：GitHub 仓库 -> Actions -> WorkBuddy 自动签到"
