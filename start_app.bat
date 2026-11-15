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

rem Порт и лог llama-server берём из .env (по умолчанию 8090 и llama_server.log)
set "LLAMA_PORT=8090"
set "LLAMA_LOG=%~dp0llama_server.log"
if exist ".env" (
    for /f "tokens=1,* delims==" %%a in ('findstr /b "LLAMACPP_PORT=" ".env"') do set "LLAMA_PORT=%%b"
    for /f "tokens=1,* delims==" %%a in ('findstr /b "LLAMACPP_LOG=" ".env"') do set "LLAMA_LOG=%~dp0%%b"
)

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
echo    [5] LLM (llama-server) status
echo    [0] Exit
echo.
set /p choice=  Choose option:
if "%choice%"=="1" goto START
if "%choice%"=="2" goto STOP
if "%choice%"=="3" goto STATUS
if "%choice%"=="4" goto LOGS
if "%choice%"=="5" goto LLAMA
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
echo        Модель llama грузится при старте приложения: статус — пункт [5] меню.
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
echo.
echo  LLM (llama-server, port %LLAMA_PORT%):
call :LLAMA_QUICK
pause
goto MENU

:LLAMA
echo.
echo  ---------- llama-server ----------
echo  Port: %LLAMA_PORT%    Log: %LLAMA_LOG%
echo.
echo  Процесс:
tasklist /fi "imagename eq llama-server.exe" 2>nul | findstr /i "llama-server" >nul
if errorlevel 1 (
    echo    llama-server.exe не запущен
) else (
    echo    llama-server.exe запущен
)
echo.
echo  Готовность (/health):
call :LLAMA_QUICK
echo.
echo  Последние строки лога:
if exist "%LLAMA_LOG%" (
    powershell -Command "Get-Content '%LLAMA_LOG%' -Tail 12"
) else (
    echo    лог пока не создан: %LLAMA_LOG%
)
echo.
pause
goto MENU

rem Одна строка статуса: 200 = готова, 503 = грузит модель, пусто = не отвечает
:LLAMA_QUICK
set "LLAMA_CODE="
for /f %%c in ('curl -s -o nul -w "%%{http_code}" --max-time 3 http://127.0.0.1:%LLAMA_PORT%/health 2^>nul') do set "LLAMA_CODE=%%c"
if "%LLAMA_CODE%"=="200" (
    echo    готова ^(200^)
) else if "%LLAMA_CODE%"=="503" (
    echo    грузит модель ^(503^), подождите
) else (
    echo    не отвечает ^(нет связи с портом %LLAMA_PORT%^)
)
goto :eof

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