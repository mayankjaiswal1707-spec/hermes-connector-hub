# Smoke test: syntax-check all sources and probe local endpoints.
# Never contacts upstream model providers.
$ErrorActionPreference = 'Continue'
$root = Split-Path (Split-Path $MyInvocation.MyCommand.Path -Parent) -Parent
$fail = 0

# Locate Python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
  $candidate = Join-Path $env:LOCALAPPDATA 'hermes\tools\python-3.14.9+20261021-win32-x64\python.exe'
  if (Test-Path $candidate) { $py = $candidate }
}
if (-not $py) { Write-Host 'FAIL: no Python found'; exit 1 }
Write-Host "Python: $py"

# 1) Syntax check
$sources = @(
  'connector\connector.py',
  'bridges\cline-bridge\proxy\proxy.py',
  'bridges\opencode-bridge\proxy\proxy.py',
  'bridges\antigravity-bridge\proxy\proxy.py',
  'bridges\gemini-bridge\proxy\proxy.py'
)
foreach ($s in $sources) {
  $f = Join-Path $root $s
  if (-not (Test-Path $f)) { Write-Host "FAIL missing: $s"; $fail++; continue }
  & $py -m py_compile $f 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Host "OK   compile $s" }
  else { Write-Host "FAIL compile $s"; $fail++ }
}

# 2) Local endpoint health (informational if services are not running)
foreach ($p in 8377, 8378, 8379, 8380, 8399) {
  try {
    $r = Invoke-RestMethod -TimeoutSec 8 "http://127.0.0.1:$p/health"
    Write-Host "OK   endpoint $p (ok=$($r.ok))"
  } catch {
    Write-Host "INFO endpoint $p not running (start services before use)"
  }
}

if ($fail -gt 0) { Write-Host "SMOKE TEST FAILED ($fail error(s))"; exit 1 }
Write-Host 'SMOKE TEST PASSED'
exit 0
