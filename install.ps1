# Installer for Hermes Connector Hub (Windows).
# Run from anywhere: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$repo = Split-Path $MyInvocation.MyCommand.Path -Parent
$hermes = Join-Path $env:LOCALAPPDATA 'hermes'

Write-Host "Hermes home: $hermes"
if (-not (Test-Path $hermes)) { throw "Hermes home not found: $hermes" }

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
  $candidate = Join-Path $hermes 'tools\python-3.14.9+20261021-win32-x64\python.exe'
  if (Test-Path $candidate) { $py = $candidate }
}
if (-not $py) { throw 'Python not found' }

# 1) Copy sources
$map = @(
  @('connector\connector.py', 'connector\connector.py'),
  @('bridges\cline-bridge\proxy\proxy.py', 'cline-bridge\proxy\proxy.py'),
  @('bridges\opencode-bridge\proxy\proxy.py', 'opencode-bridge\proxy\proxy.py'),
  @('bridges\antigravity-bridge\proxy\proxy.py', 'antigravity-bridge\proxy\proxy.py'),
  @('bridges\antigravity-bridge\proxy\start-proxy.cmd', 'antigravity-bridge\proxy\start-proxy.cmd'),
  @('bridges\gemini-bridge\proxy\proxy.py', 'gemini-bridge\proxy\proxy.py'),
  @('bridges\gemini-bridge\proxy\start-proxy.cmd', 'gemini-bridge\proxy\start-proxy.cmd'),
  @('bridges\zhipu-bridge\proxy\proxy.py', 'zhipu-bridge\proxy\proxy.py'),
  @('bridges\zhipu-bridge\proxy\start-proxy.cmd', 'zhipu-bridge\proxy\start-proxy.cmd'),
  @('bridges\nvidia-bridge\proxy\proxy.py', 'nvidia-bridge\proxy\proxy.py'),
  @('bridges\nvidia-bridge\proxy\start-proxy.cmd', 'nvidia-bridge\proxy\start-proxy.cmd')
)
foreach ($m in $map) {
  $src = Join-Path $repo $m[0]
  $dst = Join-Path $hermes $m[1]
  New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
  Copy-Item $src $dst -Force
  Write-Host "installed $($m[1])"
}

# 2) Generate bearer keys (only if missing)
$envPath = Join-Path $hermes '.env'
$lines = if (Test-Path $envPath) { Get-Content $envPath } else { @() }
foreach ($key in 'CLINE_BRIDGE_PROXY_KEY','OPENCODE_BRIDGE_PROXY_KEY','ANTI_GRAVITY_BRIDGE_PROXY_KEY') {
  if (-not ($lines | Select-String "^$key=")) {
    $value = 'brg_' + [guid]::NewGuid().ToString('N').Substring(0, 32)
    $lines += "$key=$value"
    Write-Host "generated $key"
  }
}
Set-Content -Path $envPath -Value $lines -Encoding UTF8

# 3) Register + probe
$shim = Join-Path $hermes 'connector\hermes-connectors.cmd'
if (Test-Path $shim) {
  & $shim seed
  & $shim refresh-models
  Write-Host 'Run: hermes-connectors.cmd test --all   then   hermes-connectors.cmd serve'
} else {
  Write-Host 'hermes-connectors.cmd shim not present; create it to wrap connector.py with your Python.'
}
Write-Host 'Install complete.'
