@echo off
REM === BotForge Editor === local Kinocut review dashboard + media library
set PATH=C:\Users\mayan\AppData\Local\hermes\tools\ffmpeg-9.0.1-win32-x64\bin;C:\Users\mayan\AppData\Local\hermes\tools\kinocut-venv\Scripts;%PATH%
title BotForge Editor
echo.
echo  Opening BotForge Editor...
echo  Media library : C:\Users\mayan\Videos\BotForge
echo  Review surface: C:\Users\mayan\Videos\BotForge\review\index.html
echo.
if not exist "C:\Users\mayan\Videos\BotForge\review\index.html" python3 "C:\Users\mayan\AppData\Local\hermes\profiles\social-media-manager\scripts\build_editor_dashboard.py"
start "" "C:\Users\mayan\Videos\BotForge\review\index.html"
start "" explorer.exe "C:\Users\mayan\Videos\BotForge"
echo  Dashboard opened. (Approve/reject with Hermes — it writes the decision.)
timeout /t 6 >nul
