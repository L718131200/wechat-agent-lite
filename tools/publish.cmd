@echo off
REM 一键发布到 GitHub 的入口（.ps1 会被执行策略拦，所以用这个包一层）
REM 用法：tools\publish.cmd           正式发布
REM      tools\publish.cmd -DryRun   只检查、不改远端
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish-github.ps1" %*
