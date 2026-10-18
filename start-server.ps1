# Starts the MCP server and Cloudflare tunnel
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

# 1. Check Python virtual environment
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Please setup .venv first." -ForegroundColor Red
    exit 1
}

# 2. Check .env
if (-not (Test-Path ".env")) {
    Write-Host "Generating .env with a secure secret token..." -ForegroundColor Yellow
    .\.venv\Scripts\python.exe -c "import secrets; open('.env', 'w').write(f'MFP_MCP_TOKEN={secrets.token_hex(32)}\nMFP_SYNC_BACKEND=mock\n')"
}

# 3. Read token
$tokenMatch = Get-Content .env | Select-String "^MFP_MCP_TOKEN=(.+)$"
if (-not $tokenMatch) {
    Write-Host "MFP_MCP_TOKEN not found in .env" -ForegroundColor Red
    exit 1
}
$token = $tokenMatch.Matches.Groups[1].Value.Trim()

# 4. Start MCP server in background if not already running
$serverProcess = Get-Process python, mfp-sync-mcp -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "*myfitnesslocal\.venv\*"
}

if (-not $serverProcess) {
    Write-Host "Starting MCP server on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
    $env:MFP_MCP_TOKEN = $token
    Start-Process -FilePath ".\.venv\Scripts\mfp-sync-mcp.exe" -ArgumentList "--transport streamable-http --port 8000" -WindowStyle Hidden
    Start-Sleep -Seconds 2
} else {
    Write-Host "MCP server is already running." -ForegroundColor Green
}

# 5. Display ChatGPT connection credentials
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " MCP Server is LIVE on port 8000" -ForegroundColor Green
Write-Host " Bearer Token: $token" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
Write-Host ""

# 6. Start Cloudflare tunnel
$cfPath = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (Test-Path $cfPath) {
    Write-Host "Starting Cloudflare tunnel (Ctrl+C to stop)..." -ForegroundColor Cyan
    & $cfPath tunnel --url http://127.0.0.1:8000
} else {
    Write-Host "cloudflared.exe not found. You can run 'ngrok http 8000' in another window." -ForegroundColor Yellow
}
