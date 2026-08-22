@echo off
:: ========================================================
:: Antigravity 2.0 HanHua Launcher (CDP Runtime Injection)
:: One-click: kill old instance -> start with debug port -> inject engine
:: ========================================================
title Antigravity 2.0 HanHua Launcher

echo.
echo [1/3] Closing running Antigravity...
taskkill /f /im Antigravity.exe /t >nul 2>nul
:: Wait ~2s for the process to fully exit. Use ping instead of timeout.exe:
:: timeout.exe aborts when stdin is redirected, ping never does.
ping -n 3 127.0.0.1 >nul 2>nul

echo.
echo [2/3] Starting Antigravity and injecting...
echo       (This waits for the UI to finish loading, then guards the
echo        injection for 30s. Please keep this window open until done.)
python "%~dp0hanhua_v2.py" %*

echo.
echo [3/3] Done!
echo.
echo [Note] Translation engine injected. Closing this window is OK.
echo.
pause
