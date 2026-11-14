$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait postgres
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL startup failed" }
uv run paoding init
if ($LASTEXITCODE -ne 0) { throw "Database migration failed" }
Write-Host "Ready. Run 'uv run paoding serve' and 'uv run paoding worker' in separate terminals."
