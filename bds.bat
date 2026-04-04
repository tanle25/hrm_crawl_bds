@echo off
:: bds.bat — BDS Agent CLI for Windows
:: Usage: bds.bat start | stop | restart | status | log | crawl | enrich | telegram
cd /d "%~dp0"
set PYTHONPATH=%~dp0

if "%1"=="" goto help
if "%1"=="start"   goto start
if "%1"=="stop"    goto stop
if "%1"=="restart" goto restart
if "%1"=="status"  goto status
if "%1"=="log"      goto log
if "%1"=="crawl"    goto crawl
if "%1"=="enrich"   goto enrich
if "%1"=="telegram" goto telegram
goto help

:start
    echo Khu di% BD BS Agent...
    .venv314\Scripts\python.exe -m uvicorn api_app:app --host 0.0.0.0 --port 8000 >> bds.log 2>&1 &
    echo BDS Agent da khoi dong (PID %errorlevel%)
    exit /b

:stop
    for /f "tokens=2" %%a in ('tasklist /fi "windowtitle eq BDS*" /nh /fo csv') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    echo Da dung BDS Agent
    exit /b

:restart
    call :stop
    timeout /t 2 /nobreak >nul
    call :start
    exit /b

:status
    tasklist /fi "imagename eq python.exe" /nh | findstr /i "uvicorn" >nul
    if %errorlevel%==0 (
        echo Dang chay
    ) else (
        echo Khong chay
    )
    exit /b

:log
    set LINES=30
    if not "%2"=="" set LINES=%2
    powershell -command "Get-Content bds.log -Tail %LINES%"
    exit /b

:crawl
    .venv314\Scripts\python.exe facebook_group_scraper.py
    exit /b

:enrich
    .venv314\Scripts\python.exe llm_enricher.py
    exit /b

:telegram
    .venv314\Scripts\python.exe -c "from services.telegram import get_notifier; n=get_notifier(); ok=n.send('BDS CLI OK'); print('OK' if ok else 'FAIL')"
    exit /b

:help
    echo BDS Agent CLI
    echo ========================
    echo   bds.bat start     Khoi dong server
    echo   bds.bat stop      Dung server
    echo   bds.bat restart   Restart server
    echo   bds.bat status    Trang thai
    echo   bds.bat log       Xem log (30 dong cuoi)
    echo   bds.bat log 100   Xem 100 dong cuoi
    echo   bds.bat crawl     Chay scraper
    echo   bds.bat enrich    Chay enricher
    echo   bds.bat telegram  Test Telegram
    exit /b
