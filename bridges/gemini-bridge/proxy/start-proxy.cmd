@echo off
rem Starts the Gemini bridge proxy (OpenAI-compatible over Google's Gemini API).
rem Loads GOOGLE_API_KEY and GEMINI_BRIDGE_PROXY_KEY from hermes\.env.
set "GOOGLE_API_KEY="
set "GOOGLE_API_KEY_2="
set "GEMINI_BRIDGE_PROXY_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%LOCALAPPDATA%\hermes\.env") do (
  if /i "%%a"=="GOOGLE_API_KEY" set "GOOGLE_API_KEY=%%b"
  if /i "%%a"=="GOOGLE_API_KEY_2" set "GOOGLE_API_KEY_2=%%b"
  if /i "%%a"=="GEMINI_BRIDGE_PROXY_KEY" set "GEMINI_BRIDGE_PROXY_KEY=%%b"
)
"C:\Users\mayan\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe" "%~dp0proxy.py" %*
