<#
.SYNOPSIS
    一键把 wechat-agent-lite 发布到 GitHub。

.DESCRIPTION
    本机 github.com 域名解析不通，所以推送走 SSH over 443：
        remote = ssh://git@ssh.github.com:443/<owner>/<repo>.git
    如果还没有 GitHub 专用密钥，脚本会生成一把（只用于 GitHub，不与 VPS/阿里云的混用），
    再用 API 把公钥挂到你账号上、创建仓库、推送。

    需要 E:\dsh\.keys\github-token.txt 里的 token 具备 repo 权限（创建仓库）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\publish-github.ps1 -DryRun
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\publish-github.ps1
#>
param(
    [string]$RepoName = "wechat-agent-lite",
    [string]$TokenFile = "E:\dsh\.keys\github-token.txt",
    [string]$KeyDir = "E:\dsh\.ssh",
    [string]$Description = "微信 AI 机器人的业务层：只做监听/触发/上下文/模型/识图/分段，收发交给 wechatauto-replica 引擎",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }

$repoRoot = Split-Path -Parent $PSScriptRoot
Write-Host "`n=== 发布 wechat-agent-lite 到 GitHub ===" -ForegroundColor Cyan
Info "仓库目录: $repoRoot"
Info "目标仓库: $RepoName$(if ($DryRun) { '   [DryRun 模式，不会真的改远端]' })"

# ---------- 0. 检查本地 git 状态 ----------
Push-Location $repoRoot
try {
    $dirty = git status --short
    if ($dirty) { Warn "工作区不干净，建议先提交：`n$dirty" }
    $head = git log --oneline -1 2>$null
    if (-not $head) { throw "还没有任何提交，先 git commit 再来发布" }
    Ok "本地提交: $head"
} finally { Pop-Location }

# ---------- 1. token ----------
if (-not (Test-Path $TokenFile)) { throw "找不到 token 文件：$TokenFile" }
$token = (Get-Content $TokenFile -Raw).Trim()
$headers = @{ Authorization = "token $token"; "User-Agent" = "wechat-agent-lite"; Accept = "application/vnd.github+json" }
$me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers -TimeoutSec 30
$owner = $me.login
Ok "GitHub 身份: $owner"

# ---------- 2. SSH 专用密钥 ----------
if (-not (Test-Path $KeyDir)) { New-Item -ItemType Directory -Path $KeyDir -Force | Out-Null }
$keyPath = Join-Path $KeyDir "github_ed25519"
if (Test-Path $keyPath) {
    Ok "复用已有 GitHub 密钥: $keyPath"
} else {
    if ($DryRun) { Warn "将生成新密钥 $keyPath（DryRun 下跳过）" }
    else {
        & ssh-keygen -t ed25519 -N '""' -C "wechat-agent-lite@$owner" -f $keyPath | Out-Null
        Ok "已生成 GitHub 专用密钥: $keyPath"
    }
}

# ---------- 3. 公钥挂到账号 ----------
if (Test-Path "$keyPath.pub") {
    $pub = (Get-Content "$keyPath.pub" -Raw).Trim()
    $existing = Invoke-RestMethod -Uri "https://api.github.com/user/keys" -Headers $headers -TimeoutSec 30
    $already = $existing | Where-Object { $_.key -eq $pub }
    if ($already) {
        Ok "公钥已在账号上（id=$($already.id)）"
    } elseif ($DryRun) {
        Warn "将上传公钥（DryRun 下跳过）"
    } else {
        $body = @{ title = "wechat-agent-lite ($env:COMPUTERNAME)"; key = $pub } | ConvertTo-Json
        $k = Invoke-RestMethod -Uri "https://api.github.com/user/keys" -Method Post -Headers $headers -Body $body -TimeoutSec 30
        Ok "公钥已上传（id=$($k.id)）"
    }
}

# ---------- 4. 远端仓库 ----------
$repoApi = "https://api.github.com/repos/$owner/$RepoName"
$exists = $true
try { Invoke-RestMethod -Uri $repoApi -Headers $headers -TimeoutSec 30 | Out-Null }
catch { $exists = $false }

if ($exists) {
    Ok "远端仓库已存在: https://github.com/$owner/$RepoName"
} elseif ($DryRun) {
    Warn "将创建仓库 $owner/$RepoName（DryRun 下跳过）"
} else {
    $body = @{
        name        = $RepoName
        description = $Description
        private     = $false
        has_issues  = $true
        has_wiki    = $false
        auto_init   = $false     # 本地已有历史，不要再造一个初始提交
    } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Method Post -Headers $headers -Body $body -TimeoutSec 60 | Out-Null
    Ok "已创建仓库: https://github.com/$owner/$RepoName"
}

# ---------- 5. remote 与推送 ----------
$remote = "ssh://git@ssh.github.com:443/$owner/$RepoName.git"
Push-Location $repoRoot
try {
    git remote remove origin 2>$null
    git remote add origin $remote
    # github.com 解析不通 + 中文用户名会让 Git 自带 ssh 编成 GBK，所以指定系统 ssh
    git config core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe -i $($keyPath -replace '\\','/') -o IdentitiesOnly=yes -o UserKnownHostsFile=$($KeyDir -replace '\\','/')/known_hosts"
    Ok "remote 已配置: $remote"
    Info "core.sshCommand = $(git config --get core.sshCommand)"

    if ($DryRun) {
        Warn "DryRun：跳过推送。确认无误后去掉 -DryRun 再跑一次。"
    } else {
        Info "正在推送（首次会提示确认主机指纹，本机已用 known_hosts 固定）…"
        git push -u origin main 2>&1 | ForEach-Object { "    $_" }
        if ($LASTEXITCODE -ne 0) { throw "推送失败（看上面的 git 输出）" }
        Ok "推送完成"
    }
} finally { Pop-Location }

Write-Host "`n仓库地址: https://github.com/$owner/$RepoName" -ForegroundColor Cyan
Write-Host "别忘了在仓库页面上补 topics 和 About，并在 README 已含免责声明的前提下再公开。" -ForegroundColor Cyan
