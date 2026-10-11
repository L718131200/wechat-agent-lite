@echo off
REM Re-add UTF-8 BOM to publish-github.ps1.
REM Why: Windows PowerShell 5.1 reads .ps1 as ANSI(GBK) unless a BOM is present,
REM so any Chinese comment turns into mojibake and breaks the syntax.
REM Any editor/tool that rewrites the file as UTF-8 without BOM will break it again.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%~dp0publish-github.ps1'; $c=[IO.File]::ReadAllText($p,(New-Object Text.UTF8Encoding($false))); [IO.File]::WriteAllText($p,$c,(New-Object Text.UTF8Encoding($true))); $b=[IO.File]::ReadAllBytes($p)[0..2] | ForEach-Object { $_.ToString('X2') }; Write-Host ('BOM now: ' + ($b -join ' '))"
