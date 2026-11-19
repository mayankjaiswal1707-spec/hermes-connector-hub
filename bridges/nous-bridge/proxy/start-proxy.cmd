@echo off
rem Starts the Nous Portal free-model bridge (OpenAI-compatible, port 8384).
rem Reads the Nous OAuth token from hermes\auth.json (kept fresh by Hermes).
rem Loads NOUS_BRIDGE_PROXY_KEY from hermes\.env.
set "NOUS_BRIDGE_PROXY_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
  if /i "%%a"=="NOUS_BRIDGE_PROXY_KEY" set "NOUS_BRIDGE_PROXY_KEY=%%b"
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
