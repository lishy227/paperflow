@echo off
chcp 65001 >nul
title paid-db-access - open databases
cd /d "%~dp0"

echo ============================================================
echo   paid-db-access launcher: browser + 4 databases + health
echo ============================================================
echo.

REM ---------- 1. Python check ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo         Install Python 3.9+ and check "Add Python to PATH", then retry.
    echo.
    pause
    exit /b 1
)

REM ---------- 2. Dependencies check / auto install ----------
python -c "import yaml, websocket, aiohttp" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Missing dependencies, installing requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Check your network and retry.
        echo.
        pause
        exit /b 1
    )
)

REM ---------- 3. .env check ----------
if not exist .env (
    copy .env.example .env >nul
    echo [FIRST RUN] Created .env template.
    echo             Fill in SCOPUS_API_KEY, LLM_API_KEY optional, save, then rerun.
    echo.
    notepad .env
    pause
    exit /b 1
)

REM ---------- 4. Launch browser + open 4 databases ----------
echo [LAUNCH] Opening IEEE / ACM / Scopus / EV ...
python launch_browser.py --open "https://ieeexplore.ieee.org/Xplore/home.jsp" --open "https://dl.acm.org" --open "https://www.scopus.com" --open "https://www.engineeringvillage.com/app/search/quick/"
if errorlevel 1 (
    echo [ERROR] Browser launch failed. See logs above.
    echo.
    pause
    exit /b 1
)

REM ---------- 5. Health check ----------
echo.
echo [CHECK] Running health check ...
python scripts/health_check.py
if errorlevel 1 (
    echo.
    echo [NOTE] Some checks failed, usually expired login.
    echo        Browser is open - sign in / pass captcha there, then rerun this script.
)

echo.
pause
