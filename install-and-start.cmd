@echo off
where pwsh.exe >nul 2>&1
if errorlevel 1 (
  echo PowerShell 7 was not found. Install PowerShell 7 and retry.
  pause
  exit /b 1
)
pwsh.exe -NoProfile -STA -File "%~dp0install-and-start.ps1"
if errorlevel 1 pause
