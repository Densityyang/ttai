$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
foreach ($name in @('tt-ai','tt-api','tunnel')) {
  $pf = Join-Path $root "var\$name.pid"
  if (Test-Path $pf) {
    $procId = Get-Content $pf | Select-Object -First 1
    try { Stop-Process -Id ([int]$procId) -Force -ErrorAction Stop; Write-Host "[$name] stopped pid=$procId" }
    catch { Write-Host "[$name] pid=$procId not running" }
    Remove-Item $pf -Force -ErrorAction SilentlyContinue
  } else { Write-Host "[$name] no pid file" }
}
Write-Host 'stop-local-demo done'
