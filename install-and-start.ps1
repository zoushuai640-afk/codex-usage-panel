$ErrorActionPreference = 'Stop'
$embedded = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $embedded) -and -not (Get-Command python.exe -ErrorAction SilentlyContinue)) { throw 'Python 3.10+ was not found. Install Python or use Codex bundled Python, then retry.' }
$exe = (Get-Process -Id $PID).Path
$float = Join-Path $PSScriptRoot 'floating-status.ps1'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Codex Token Floating Bar.lnk'
$shellLink = New-Object -ComObject WScript.Shell
$lnk = $shellLink.CreateShortcut($shortcutPath)
$lnk.TargetPath = $exe
$lnk.Arguments = '-NoProfile -STA -WindowStyle Hidden -File "' + $float + '"'
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.WindowStyle = 7
$lnk.Save()
Start-Process -FilePath $exe -ArgumentList @('-NoProfile','-STA','-WindowStyle','Hidden','-File',('"'+$float+'"')) -WindowStyle Hidden
Write-Host 'Installed and started. Windows sign-in startup is enabled.'
