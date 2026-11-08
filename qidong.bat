@echo off
setlocal
:: ========================================================
:: Antigravity 2.0 HanHua Launcher (CDP Runtime Injection)
:: Reuse a verified instance, or start one when none is running.
:: ========================================================
title Antigravity 2.0 HanHua Launcher

echo.
echo [1/2] Finding Antigravity and injecting safely...
echo       (This waits for the UI to finish loading, then guards the
echo        injection for 30s. Please keep this window open until done.)
python "%~dp0hanhua_v2.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" goto failed

echo.
echo [2/2] Done!
echo.
echo [Note] Translation engine injected. Closing this window is OK.
echo.
pause
exit /b 0

:failed
echo.
echo [Error] Translation failed. Review the message above.
pause
exit /b %RESULT%
