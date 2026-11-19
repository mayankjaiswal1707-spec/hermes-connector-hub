@echo off
rem Starts the NVIDIA NIM bridge proxy (OpenAI-compatible, port 8382).
rem Loads NVIDIA_API_KEY and NVIDIA_BRIDGE_PROXY_KEY from hermes\.env.
set "NVIDIA_API_KEY="
set "NVIDIA_BRIDGE_PROXY_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
  if /i "%%a"=="NVIDIA_API_KEY" set "NVIDIA_API_KEY=%%b"
  if /i "%%a"=="NVIDIA_BRIDGE_PROXY_KEY" set "NVIDIA_BRIDGE_PROXY_KEY=%%b"
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
