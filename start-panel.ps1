param([int]$Port = 8767)
$ErrorActionPreference = 'Stop'
$python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$address = "http://127.0.0.1:$Port"
$health = $null
try { $health = Invoke-RestMethod "$address/health" -TimeoutSec 2 -NoProxy } catch { }
if ($health) {
    if ($health.app -eq 'codex-token-panel-v1') { Write-Output $address; exit 0 }
    throw '该端口已被其他程序占用。'
}
if (-not (Test-Path -LiteralPath $python)) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue; if (-not $cmd) { throw '请安装 Python 3.10 或更新版本。' }; $python = $cmd.Source }
$runtime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$process = Start-Process -FilePath $python -ArgumentList @('-u', ('"' + (Join-Path $PSScriptRoot 'server.py') + '"'), '--port', $Port) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'server.log') -RedirectStandardError (Join-Path $runtime 'server-error.log')
$process.Id | Set-Content -LiteralPath (Join-Path $runtime 'server.pid')
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) { throw (Get-Content -LiteralPath (Join-Path $runtime 'server-error.log') -Raw) }
    try { if ((Invoke-RestMethod "$address/health" -TimeoutSec 1 -NoProxy).app -eq 'codex-token-panel-v1') { Write-Output $address; exit 0 } } catch { }
}
throw '面板启动超时，请查看 runtime/server-error.log。'
