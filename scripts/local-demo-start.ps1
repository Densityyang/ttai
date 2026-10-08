# Start the local real-data demo: tunnel + V4 API (9001) + TT Admin API (9000).
# Configuration is injected as PROCESS ENV so the repository keeps NO auto-loaded
# .env (which would change the test baseline).
param([switch]$NoTunnel)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

$py = Join-Path $root '.venv\Scripts\python.exe'

# Ambient proxy vars (a bracketed NO_PROXY entry) crash httpx URL parsing.
foreach ($n in 'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NO_PROXY','no_proxy') {
  Remove-Item "Env:$n" -ErrorAction SilentlyContinue
}

if (-not $NoTunnel) {
  $listening = $false
  try { $listening = [bool](Get-NetTCPConnection -LocalPort 15432 -State Listen -ErrorAction Stop) } catch { $listening = $false }
  if (-not $listening) {
    $t = Start-Process -FilePath $py -ArgumentList 'scripts\local_tunnel.py' -WorkingDirectory $root -RedirectStandardOutput (Join-Path $root 'logs\tunnel.out.log') -RedirectStandardError (Join-Path $root 'logs\tunnel.err.log') -WindowStyle Hidden -PassThru
    Write-Host "[tunnel] started pid=$($t.Id)"
    Start-Sleep -Seconds 4
  } else {
    Write-Host "[tunnel] already listening on 15432"
  }
}

# ---- V4 API (root project) ----
$env:ENV = 'dev'
$env:PYTHONUTF8 = '1'
$env:SERVICE_MODE = 'infra-dev'
$env:AUTH_ENABLED = 'false'
$env:MEMORY_BACKEND = 'memory'
$env:TYPED_RUNTIME_ACTIVATION = 'local_real_data_demo'
$env:LOCAL_REAL_DEMO_USER_ID = 'local-real-demo'
$env:LOCAL_DEMO_CERTIFICATION_ADMIN_USER_ID = 'local-real-demo'
$env:DATABASE_URL_FILE = 'secrets/database/business_ro_database_url'
$env:NL2SQL_DB_SCHEMA = 'ai_views'
$env:AI_VIEWS_CONFIG_PATH = 'configs/semantic/ai_views.yaml'
$env:AI_VIEWS_AUTO_SYNC = 'false'
$env:ENGINE_MODE = 'v2'
$env:MODEL_PROFILE_VERSION = 'v1'
$env:DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
$env:DEEPSEEK_FLASH_MODEL = 'deepseek-v4-flash'
$env:DEEPSEEK_PRO_MODEL = 'deepseek-v4-pro'
$env:DEEPSEEK_API_KEY_FILE = 'secrets/providers/deepseek_api_key'
$env:OPENAI_BASE_URL = 'https://api.deepseek.com/v1'
$env:OPENAI_API_KEY_FILE = 'secrets/providers/deepseek_api_key'
$env:MODEL_NAME = 'deepseek-v4-flash'
$env:MODEL_REQUIRED = 'true'
$env:SKIP_RAG_STARTUP_SYNC = 'true'
$env:RAG_STARTUP_SYNC_STRICT = 'false'
$env:SEMANTIC_RETRIEVER_MODE = 'direct'
$env:CORS_ALLOWED_ORIGINS = 'http://localhost:5000,http://127.0.0.1:5000'
$env:CORS_ALLOW_CREDENTIALS = 'false'
$env:API_PORT = '9001'
$env:LOG_LEVEL = 'INFO'
$env:V2_REQUEST_DEADLINE_MS = '120000'
$env:V2_TOKEN_BUDGET = '64000'
$env:V2_ROUTE_DEADLINE_FAST_MS = '120000'
$env:V2_ROUTE_DEADLINE_STANDARD_MS = '120000'
$env:V2_ROUTE_DEADLINE_DEEP_MS = '120000'
$ai = Start-Process -FilePath $py -ArgumentList 'main.py','prod' -WorkingDirectory $root -RedirectStandardOutput (Join-Path $root 'logs\tt-ai.out.log') -RedirectStandardError (Join-Path $root 'logs\tt-ai.err.log') -WindowStyle Hidden -PassThru
Write-Host "[tt-ai] started pid=$($ai.Id)"

# ---- TT Admin API ----
$env:ENV = 'prod'
$apiDir = Join-Path $root 'tt-intelligent-main\tt-api'
$api = Start-Process -FilePath $py -ArgumentList 'main.py','run' -WorkingDirectory $apiDir -RedirectStandardOutput (Join-Path $root 'logs\tt-api.out.log') -RedirectStandardError (Join-Path $root 'logs\tt-api.err.log') -WindowStyle Hidden -PassThru
Write-Host "[tt-api] started pid=$($api.Id)"

foreach ($i in 1..30) {
  Start-Sleep -Seconds 3
  try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9001/readyz' -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { Write-Host '[tt-ai] READY'; break } } catch {}
}
Write-Host 'start-local-demo done'
