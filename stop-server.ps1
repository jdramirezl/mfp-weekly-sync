# Stops the MCP server and tunnel processes
Write-Host "Stopping MCP server and tunnel..." -ForegroundColor Yellow
Get-Process mfp-sync-mcp, cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*myfitnesslocal\.venv\*" } | Stop-Process -Force
Write-Host "All processes stopped." -ForegroundColor Green
