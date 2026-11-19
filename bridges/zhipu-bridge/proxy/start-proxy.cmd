@echo off
rem Starts the Zhipu GLM free-tier bridge (OpenAI-compatible, port 8381).
rem Loads ZHIPU_API_KEY and ZHIPU_BRIDGE_PROXY_KEY from hermes\.env.
set "ZHIPU_API_KEY="
set "ZHIPU_BRIDGE_PROXY_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
  if /i "%%a"=="ZHIPU_API_KEY" set "ZHIPU_API_KEY=%%b"
  if /i "%%a"=="ZHIPU_BRIDGE_PROXY_KEY" set "ZHIPU_BRIDGE_PROXY_KEY=%%b"
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
