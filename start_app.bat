@echo off
title Local AI Assistant - Controller
chcp 65001 >nul 2>&1
setlocal

rem Папка проекта — та, где лежит этот файл
cd /d "%~dp0"
set "LOG_FILE=%~dp0assistant.log"
set "SEARXNG_DIR=%~dp0searxng"
set "COMPOSE=docker compose"
where docker-compose >nul 2>&1 && set "COMPOSE=docker-compose"

if not exist "docker-compose.yml" (
    echo ERROR: docker-compose.yml not found in %CD%
    pause & exit /b 1
)
rem .env создаём из шаблона при первом запуске (поиск и настройки берутся из него)
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul
where docker >nul 2>&1 || (
    echo ERROR: Docker not found. Install Docker Desktop.
    pause & exit /b 1
)

:MENU
cls
echo.
echo  ==============================================================
echo                   LOCAL AI ASSISTANT - CONTROLLER
echo  ==============================================================
echo.
echo    [1] Start server
echo    [2] Stop server
echo    [3] Status
echo    [4] Show last log lines
echo    [0] Exit
echo.
set /p choice=  Choose option:
if "%choice%"=="1" goto START
if "%choice%"=="2" goto STOP
if "%choice%"=="3" goto STATUS
if "%choice%"=="4" goto LOGS
if "%choice%"=="0" goto EXIT
goto MENU

:START
echo.
echo  [1/3] Starting nginx (Docker)...
%COMPOSE% up -d
if errorlevel 1 (
    echo  ERROR: could not start nginx container.
    pause & goto MENU
)

echo  [2/3] Activating environment...
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo        no .venv found, using system Python
)

call :START_SEARXNG

echo  [3/3] Starting application on http://127.0.0.1:8000
echo        Press Ctrl+C to stop.
echo.
python run.py
set "EXIT_CODE=%errorlevel%"

echo.
echo  Application stopped (code %EXIT_CODE%). Stopping nginx and SearXNG...
%COMPOSE% down
call :STOP_SEARXNG
if not "%EXIT_CODE%"=="0" (
    echo %date% %time% - application exited with code %EXIT_CODE% >> "%LOG_FILE%"
)
pause
goto MENU

:STOP
echo.
%COMPOSE% down
call :STOP_SEARXNG
echo  Containers stopped.
pause
goto MENU

:STATUS
echo.
echo  Docker containers:
%COMPOSE% ps
if exist "%SEARXNG_DIR%\docker-compose.yml" (
    pushd "%SEARXNG_DIR%"
    %COMPOSE% ps
    popd
)
echo.
echo  Port 8000:
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo    not listening
) else (
    echo    listening
)
echo.
echo  Health check:
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/health
echo.
pause
goto MENU

:LOGS
echo.
if exist "%LOG_FILE%" (
    powershell -Command "Get-Content '%LOG_FILE%' -Tail 20"
) else (
    echo  No log file yet: %LOG_FILE%
)
echo.
pause
goto MENU

:EXIT
%COMPOSE% down >nul 2>&1
call :STOP_SEARXNG >nul 2>&1
exit /b 0

rem ---------- SearXNG (поиск в интернете, бесплатно) ----------
:START_SEARXNG
if not exist "%SEARXNG_DIR%\docker-compose.yml" (
    echo        SearXNG not found, поиск пойдёт через DuckDuckGo
    goto :eof
)
rem секрет SearXNG создаём один раз, файл searxng\.env не попадает в git
if not exist "%SEARXNG_DIR%\.env" (
    python -c "import secrets; print('SEARXNG_SECRET=' + secrets.token_hex(32))" > "%SEARXNG_DIR%\.env"
)
echo        Starting SearXNG on http://127.0.0.1:8080 ...
pushd "%SEARXNG_DIR%"
%COMPOSE% up -d
if errorlevel 1 echo        WARNING: SearXNG не запустился, поиск пойдёт через DuckDuckGo
popd
goto :eof

:STOP_SEARXNG
if not exist "%SEARXNG_DIR%\docker-compose.yml" goto :eof
pushd "%SEARXNG_DIR%"
%COMPOSE% down
popd
goto :eof