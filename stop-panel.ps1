param([int]$Port = 8767)
$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'runtime\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { exit 0 }
$serverProcessId = [int](Get-Content -LiteralPath $pidFile)
$process = Get-Process -Id $serverProcessId -ErrorAction SilentlyContinue
if ($process) {
    $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 -NoProxy
    if ($health.app -ne 'codex-token-panel-v1' -or $health.pid -ne $serverProcessId) { throw '进程身份不匹配，未停止任何程序。' }
    Stop-Process -Id $serverProcessId
}
Remove-Item -LiteralPath $pidFile
