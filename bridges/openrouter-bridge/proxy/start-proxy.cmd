@echo off
rem Starts the OpenRouter free-model bridge (native pass-through, port 8383).
rem Loads OPENROUTER_API_KEY and OPENROUTER_BRIDGE_PROXY_KEY from hermes\.env.
set "OPENROUTER_API_KEY="
set "OPENROUTER_BRIDGE_PROXY_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
  if /i "%%a"=="OPENROUTER_API_KEY" set "OPENROUTER_API_KEY=%%b"
  if /i "%%a"=="OPENROUTER_BRIDGE_PROXY_KEY" set "OPENROUTER_BRIDGE_PROXY_KEY=%%b"
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
