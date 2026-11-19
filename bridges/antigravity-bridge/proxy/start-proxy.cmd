@echo off
rem Starts the antigravity-bridge proxy (OpenAI-compatible endpoint over the Antigravity CLI `agy`).
rem Default port 8379. Override: set ANTI_GRAVITY_BRIDGE_PROXY_PORT=NNNN before calling.
rem Bearer key: ANTI_GRAVITY_BRIDGE_PROXY_KEY (validated; set in hermes\.env).
set ANTI_GRAVITY_BRIDGE_PROXY_KEY=%ANTI_GRAVITY_BRIDGE_PROXY_KEY%
if "%ANTI_GRAVITY_BRIDGE_PROXY_KEY%"=="" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
    if /i "%%a"=="ANTI_GRAVITY_BRIDGE_PROXY_KEY" set "ANTI_GRAVITY_BRIDGE_PROXY_KEY=%%b"
  )
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
