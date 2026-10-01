$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'This launcher is for Windows.'
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath '.env')) {
    throw 'Create .env from .env.example and set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS.'
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Install dependencies first: py -3.12 -m uv sync --group dev'
}

& $python -c 'from telegram_bot.core.services.providers import CODEX_ADAPTER; print("Codex CLI:", CODEX_ADAPTER.safe_binary() or "not found")'
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect Codex CLI.'
}

& $python -m telegram_bot
exit $LASTEXITCODE
