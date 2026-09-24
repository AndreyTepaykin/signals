# API dev server helpers for Windows.
#
# Native uvicorn --reload often hangs at "Reloading..." when the console is
# not focused (Cursor agent shells, Start-Process, background terminals).
# Workaround: run in a visible Cursor/Windows Terminal and click it if stuck.
#
# Usage:
#   .\run.ps1              # reliable: watchfiles restarts whole process
#   .\run.ps1 -NativeReload  # uvicorn --reload (interactive terminal only)

param(
  [switch]$NativeReload
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$env:PYTHONUNBUFFERED = '1'
$env:WATCHFILES_FORCE_POLLING = '1'

$listeners = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($row in $listeners) {
  $procId = $row.OwningProcess
  if ($procId -and $procId -ne 0) {
    Write-Host "Stopping old process on :8000 (PID $procId)"
    taskkill /F /PID $procId 2>$null | Out-Null
  }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if ($NativeReload) {
  Write-Host "Starting with native --reload (keep this terminal focused)."
  & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
} else {
  Write-Host "Starting with watchfiles process restart (recommended on Win10)."
  $uvicornCmd = "$python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
  & $python -m watchfiles --filter python --verbosity info $uvicornCmd app
}
