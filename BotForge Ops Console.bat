@echo off
REM === BotForge Ops Console === status / doctor / qc / brief
set PATH=C:\Users\mayan\AppData\Local\hermes\tools\ffmpeg-9.0.1-win32-x64\bin;C:\Users\mayan\AppData\Local\hermes\tools\kinocut-venv\Scripts;%PATH%
cd /d "C:\Users\mayan\AppData\Local\hermes\profiles\social-media-manager"
title BotForge Ops Console
:menu
echo.
echo  1) status   2) doctor   3) brief   4) qc file   5) exit
set /p c=choose:
if "%c%"=="1" python3 scripts\ops.py status
if "%c%"=="2" python3 scripts\ops.py doctor
if "%c%"=="3" python3 scripts\ops.py brief
if "%c%"=="4" set /p f=file: & python3 scripts\ops.py qc "%f%"
if "%c%"=="5" exit
if not "%c%"=="5" goto menu
